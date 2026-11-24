//go:build testkit

// IntentQoS Test Kit: measures what a household feels (delay under load) with IntentQoS switched off and on,
// on the real line, and writes a report. It drives the router's bounded "no-QoS test window" (/api/bypass) so
// both halves of a comparison run back to back on the same line, minutes apart.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"html/template"
	"io"
	"math"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const partTitle = "IntentQoS Test Kit"

// ------------------------------------------------------------------ endpoints

type endpoints struct {
	// UDP probe targets (public DNS resolvers): a small query every few hundred ms to each, the way a call or a
	// game sends small regular packets, so the router treats it like one; the reply time is the round trip
	// through every queue on the way.
	Probe []string `json:"probe"`
	Down  string   `json:"down"` // + byte count
	Up    string   `json:"up"`
	Small string   `json:"small"` // a 1-byte object on the same server (RPM probes)
}

func defaultEndpoints() endpoints {
	e := endpoints{Probe: []string{"1.1.1.1:53", "8.8.8.8:53", "9.9.9.9:53"},
		Down: "https://speed.cloudflare.com/__down?bytes=", Up: "https://speed.cloudflare.com/__up",
		Small: "https://speed.cloudflare.com/__down?bytes=1"}
	if s := os.Getenv("INTENTQOS_TEST_ENDPOINTS"); s != "" {
		_ = json.Unmarshal([]byte(s), &e)
	}
	return e
}

// ------------------------------------------------------------------ router API

type router struct {
	IP, Key string
	c       *http.Client
}

func newRouter(ip, key string) *router {
	return &router{IP: ip, Key: key, c: &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{Proxy: nil}}}
}

func (r *router) call(method, path string, body, out any) error {
	return callJSON(r.c, method, "http://"+r.IP+":8080"+path, r.Key, body, out)
}

// bypass opens or closes the router's no-QoS test window. A router whose CPU is busy (the end of a loaded
// measurement) can answer late, so a timeout is retried a few times before the test gives up: found in the
// full-system emulation, where one slow answer stopped the whole run after its first test.
func (r *router) bypass(on bool, minutes float64) error {
	var err error
	for i, wait := range []time.Duration{0, 2 * time.Second, 4 * time.Second, 8 * time.Second} {
		if i > 0 {
			logf("router did not answer the test-window request (%v); trying again in %v", err, wait)
			time.Sleep(wait)
		}
		err = r.call("POST", "/api/bypass", map[string]any{"on": on, "minutes": minutes, "by": "Test Kit on " + machineName()}, nil)
		if err == nil {
			return nil
		}
	}
	return err
}

// ------------------------------------------------------------------ measurement

type sample struct {
	T   float64 `json:"t"`   // seconds since the run started
	RTT float64 `json:"rtt"` // ms; -1 = timeout
}

type arm struct {
	QoS      string     `json:"qos"` // "off" | "on"
	Samples  []sample   `json:"samples"`
	DownMbps float64    `json:"down_mbps"`
	UpMbps   float64    `json:"up_mbps"`
	P50      float64    `json:"p50"`
	P95      float64    `json:"p95"`
	P99      float64    `json:"p99"`
	Loss     float64    `json:"loss_pct"`
	Started  time.Time  `json:"started"`
	Quality  *quality   `json:"quality,omitempty"`
	RPM      *rpmResult `json:"rpm,omitempty"`
}

type scenario struct {
	ID, Name, Desc string
	Down, Up       int // parallel streams
}

var scenarios = []scenario{
	{"call_backup", "Work call while a backup uploads", "upload at full speed (a cloud backup) while measuring the delay a call would feel", 0, 4},
	{"game_downloads", "Game while big downloads run", "downloads at full speed (game updates, a film) while measuring the delay a game would feel", 6, 0},
	{"busy_home", "Everyone online at once", "downloads and uploads together, the evening rush", 6, 4},
	{"rpm", "Responsiveness (RPM)", "the IETF responsiveness test: round trips per minute while the line is fully loaded (download)", 0, 0},
	{"home_network", "Home network only", "capacity and responsiveness between this laptop and the Operator Console, over Wi-Fi or cable, without the internet", 0, 0},
}

type live struct {
	mu       sync.Mutex
	Running  bool             `json:"running"`
	Phase    string           `json:"phase"`
	Progress float64          `json:"progress"`
	Recent   []sample         `json:"recent"`
	DownMbps float64          `json:"down_mbps"`
	UpMbps   float64          `json:"up_mbps"`
	Results  []map[string]any `json:"results"`
	Error    string           `json:"error,omitempty"`
	Report   string           `json:"report,omitempty"`
	Uploaded string           `json:"uploaded,omitempty"`
	Waiting  string           `json:"waiting,omitempty"` // failover: what the person should do now
	Done     bool             `json:"done"`
}

var L = &live{}

func (l *live) set(f func(*live)) { l.mu.Lock(); f(l); l.mu.Unlock() }

// measure runs probes and load for d and returns the samples and throughput.
func measure(ctx context.Context, ep endpoints, down, up int, d time.Duration, t0 time.Time, onSample func(sample), rc *rpmCollector) ([]sample, float64, float64) {
	ctx, cancel := context.WithTimeout(ctx, d)
	defer cancel()
	var dn, upb atomic.Int64
	var wg sync.WaitGroup
	tr := &http.Transport{Proxy: http.ProxyFromEnvironment, MaxIdleConnsPerHost: 16, DisableCompression: true}
	hc := &http.Client{Transport: tr}
	for i := 0; i < down; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			buf := make([]byte, 64<<10)
			for ctx.Err() == nil {
				req, _ := http.NewRequestWithContext(ctx, "GET", ep.Down+"200000000", nil)
				resp, err := hc.Do(req)
				if err != nil {
					sleepCtx(ctx, 500*time.Millisecond)
					continue
				}
				for {
					n, err := resp.Body.Read(buf)
					dn.Add(int64(n))
					if err != nil {
						break
					}
				}
				resp.Body.Close()
			}
		}()
	}
	for i := 0; i < up; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for ctx.Err() == nil {
				body := &zeroReader{n: 25 << 20, ctx: ctx, count: &upb}
				req, _ := http.NewRequestWithContext(ctx, "POST", ep.Up, body)
				req.ContentLength = body.n
				req.Header.Set("Content-Type", "application/octet-stream")
				resp, err := hc.Do(req)
				if err != nil {
					sleepCtx(ctx, 500*time.Millisecond)
					continue
				}
				_, _ = io.Copy(io.Discard, resp.Body)
				resp.Body.Close()
			}
		}()
	}
	var mu sync.Mutex
	var out []sample
	record := func(rtt float64) {
		smp := sample{T: math.Round(time.Since(t0).Seconds()*100) / 100, RTT: math.Round(rtt*100) / 100}
		mu.Lock()
		out = append(out, smp)
		mu.Unlock()
		if onSample != nil {
			onSample(smp)
		}
	}
	period := time.Duration(len(ep.Probe)) * 100 * time.Millisecond // 10 probes a second in total
	for i, target := range ep.Probe {
		wg.Add(1)
		go func(i int, target string) {
			defer wg.Done()
			sleepCtx(ctx, time.Duration(i)*100*time.Millisecond) // stagger the targets
			udpProbe(ctx, target, period, record)
		}(i, target)
	}
	if rc != nil && ep.Small != "" && (down > 0 || up > 0) {
		go rc.run(ctx, []*http.Client{hc}, 10) // RPM probes on the scenario's own load (fixed load, not the draft's ramp)
	}
	start := time.Now()
	// throughput readout every second
	go func() {
		var pd, pu int64
		for ctx.Err() == nil {
			sleepCtx(ctx, time.Second)
			cd, cu := dn.Load(), upb.Load()
			L.set(func(l *live) { l.DownMbps = float64(cd-pd) * 8 / 1e6; l.UpMbps = float64(cu-pu) * 8 / 1e6 })
			pd, pu = cd, cu
		}
	}()
	<-ctx.Done()
	el := time.Since(start).Seconds()
	tr.CloseIdleConnections()
	wg.Wait()
	mu.Lock()
	defer mu.Unlock()
	sort.Slice(out, func(a, b int) bool { return out[a].T < out[b].T })
	return out, float64(dn.Load()) * 8 / 1e6 / el, float64(upb.Load()) * 8 / 1e6 / el
}

// udpProbe sends a DNS query for a fixed, always-cached name every period over one UDP socket (one steady flow,
// like a call) and reports each reply time; a reply missing after 2 s counts as a timeout (-1).
func udpProbe(ctx context.Context, target string, period time.Duration, record func(float64)) {
	if !strings.Contains(target, ":") {
		target += ":53"
	}
	c, err := net.Dial("udp", target)
	if err != nil {
		return
	}
	defer c.Close()
	var mu sync.Mutex
	sent := map[uint16]time.Time{}
	go func() {
		buf := make([]byte, 1500)
		for {
			_ = c.SetReadDeadline(time.Now().Add(500 * time.Millisecond))
			n, err := c.Read(buf)
			if ctx.Err() != nil {
				return
			}
			if err != nil || n < 2 {
				continue
			}
			id := uint16(buf[0])<<8 | uint16(buf[1])
			mu.Lock()
			t, ok := sent[id]
			delete(sent, id)
			mu.Unlock()
			if ok {
				record(float64(time.Since(t).Microseconds()) / 1000)
			}
		}
	}()
	tick := time.NewTicker(period)
	defer tick.Stop()
	var id uint16 = uint16(time.Now().UnixNano())
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			id++
			mu.Lock()
			for k, t := range sent {
				if time.Since(t) > 2*time.Second {
					delete(sent, k)
					record(-1)
				}
			}
			sent[id] = time.Now()
			mu.Unlock()
			_, _ = c.Write(dnsQuery(id))
		}
	}
}

// dnsQuery builds a standard query for the A record of example.com (cached by every public resolver).
func dnsQuery(id uint16) []byte {
	q := []byte{byte(id >> 8), byte(id), 0x01, 0x00, 0, 1, 0, 0, 0, 0, 0, 0}
	for _, l := range []string{"example", "com"} {
		q = append(q, byte(len(l)))
		q = append(q, l...)
	}
	return append(q, 0, 0, 1, 0, 1)
}

type zeroReader struct {
	n, sent int64
	ctx     context.Context
	count   *atomic.Int64
}

var zeros = make([]byte, 64<<10)

func (z *zeroReader) Read(p []byte) (int, error) {
	if z.ctx.Err() != nil {
		return 0, z.ctx.Err()
	}
	if z.sent >= z.n {
		return 0, io.EOF
	}
	k := int64(len(p))
	if k > int64(len(zeros)) {
		k = int64(len(zeros))
	}
	if z.n-z.sent < k {
		k = z.n - z.sent
	}
	copy(p, zeros[:k])
	z.sent += k
	z.count.Add(k)
	return int(k), nil
}

func sleepCtx(ctx context.Context, d time.Duration) {
	select {
	case <-ctx.Done():
	case <-time.After(d):
	}
}

func stats(a *arm) {
	var v []float64
	lost := 0
	for _, s := range a.Samples {
		if s.RTT < 0 {
			lost++
			continue
		}
		v = append(v, s.RTT)
	}
	sort.Float64s(v)
	q := func(p float64) float64 {
		if len(v) == 0 {
			return 0
		}
		return math.Round(v[int(math.Min(float64(len(v)-1), math.Floor(p*float64(len(v)-1)+0.5)))]*10) / 10
	}
	a.P50, a.P95, a.P99 = q(0.50), q(0.95), q(0.99)
	if n := len(a.Samples); n > 0 {
		a.Loss = math.Round(float64(lost)/float64(n)*1000) / 10
	}
	a.DownMbps, a.UpMbps = math.Round(a.DownMbps*10)/10, math.Round(a.UpMbps*10)/10
	assess(a)
}

// ------------------------------------------------------------------ a full test

type testOpts struct {
	Scenarios []string `json:"scenarios"`
	Seconds   int      `json:"seconds"`
	Router    string   `json:"router"`
	Key       string   `json:"key"`
}

type scenarioResult struct {
	ID   string      `json:"id"`
	Name string      `json:"name"`
	Desc string      `json:"desc"`
	Off  arm         `json:"off"`
	On   arm         `json:"on"`
	Home *homeResult `json:"home,omitempty"`
}

type homeResult struct {
	Capacity *lanCapacity `json:"capacity,omitempty"`
	RPM      *rpmResult   `json:"rpm,omitempty"`
	Error    string       `json:"error,omitempty"`
	Console  string       `json:"console,omitempty"`
}

// rpmArm runs the draft's responsiveness test (download, connection ramp) with the UDP delay probes alongside.
func rpmArm(ctx context.Context, qos string, ep endpoints, secs int, t0 time.Time) arm {
	a := arm{QoS: qos, Started: time.Now()}
	limit := time.Duration(secs+6) * time.Second
	if limit < 30*time.Second {
		limit = 30 * time.Second
	}
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		a.Samples, _, _ = measure(ctx, ep, 0, 0, limit, t0, nil, nil)
	}()
	a.RPM = runRPM(ctx, ep.Down+"200000000", ep.Small, ep.Up, "", false, limit, func(m string) {
		L.set(func(l *live) { l.Phase = "Responsiveness (" + qos + "): " + m })
	})
	wg.Wait()
	if a.RPM != nil {
		a.DownMbps = a.RPM.GoodputMb
	}
	stats(&a)
	return a
}

// homeNetwork measures between this laptop and the Operator Console: RFC 9097-method capacity, then RPM.
func homeNetwork(ctx context.Context, secs int) (*homeResult, error) {
	prof := loadProfile()
	if prof.Console == nil {
		return nil, errors.New("pair with the Operator Console first: this test measures the home network to it")
	}
	ci := prof.Console
	if fresh, err := consoleAt(ci.IP); err == nil && fresh.Lab != nil {
		ci = fresh
	}
	if ci.Lab == nil {
		return nil, errors.New("this Operator Console does not offer the home-network test endpoints (update it)")
	}
	h := &homeResult{Console: ci.IP}
	d := time.Duration(secs) * time.Second
	if d > 15*time.Second {
		d = 15 * time.Second
	}
	c, err := capacityTest(ctx, ci.IP, ci.Lab.UDPPort, d, func(m float64) {
		L.set(func(l *live) { l.Phase = fmt.Sprintf("Home network: capacity, now %.0f Mbit/s", m) })
	})
	if err != nil {
		h.Error = err.Error()
	} else {
		h.Capacity = &c
	}
	urls, err := labConfig(pinnedClient(ci.Lab.Fingerprint, 1)[0], ci.IP)
	if err != nil {
		h.Error = strings.TrimSpace(h.Error + " RPM: " + err.Error())
		return h, nil
	}
	h.RPM = runRPM(ctx, urls["large_download_url"], urls["small_download_url"], urls["upload_url"], ci.Lab.Fingerprint, false,
		30*time.Second, func(m string) { L.set(func(l *live) { l.Phase = "Home network: " + m }) })
	return h, nil
}

func runTest(ctx context.Context, o testOpts, ep endpoints) ([]scenarioResult, error) {
	rt := newRouter(o.Router, o.Key)
	var st map[string]any
	if err := rt.call("GET", "/api/state", nil, &st); err != nil {
		return nil, fmt.Errorf("cannot read the router (%s): %v", o.Router, err)
	}
	if o.Seconds < 10 {
		o.Seconds = 10
	}
	var picked []scenario
	for _, s := range scenarios {
		for _, id := range o.Scenarios {
			if id == s.ID {
				picked = append(picked, s)
			}
		}
	}
	if len(picked) == 0 {
		return nil, errors.New("choose at least one test")
	}
	defer func() { _ = rt.bypass(false, 0) }() // never leave the router without QoS
	total := float64(len(picked) * 2 * (o.Seconds + 6))
	doneSec := 0.0
	var res []scenarioResult
	t0 := time.Now()
	for _, sc := range picked {
		r := scenarioResult{ID: sc.ID, Name: sc.Name, Desc: sc.Desc}
		if sc.ID == "home_network" { // inside the home only: QoS on the internet line does not change it, run once
			L.set(func(l *live) { l.Phase = sc.Name + ": capacity and responsiveness to the Operator Console" })
			hn, err := homeNetwork(ctx, o.Seconds)
			if err != nil {
				hn = &homeResult{Error: err.Error()}
			}
			r.Home = hn
			doneSec += float64(2 * (o.Seconds + 6))
			L.set(func(l *live) { l.Progress = doneSec / total })
			res = append(res, r)
			L.set(func(l *live) { l.Results = append(l.Results, summarize(r)) })
			continue
		}
		for _, qos := range []string{"off", "on"} {
			if ctx.Err() != nil {
				return res, ctx.Err()
			}
			if qos == "off" {
				if err := rt.bypass(true, float64(o.Seconds+30)/60); err != nil {
					return res, fmt.Errorf("could not open the no-QoS test window on the router: %v", err)
				}
			} else if err := rt.bypass(false, 0); err != nil {
				return res, fmt.Errorf("could not switch IntentQoS back on: %v", err)
			}
			label := map[string]string{"off": "IntentQoS off", "on": "IntentQoS on"}[qos]
			var a arm
			if sc.ID == "rpm" {
				L.set(func(l *live) { l.Phase = sc.Name + ": " + label })
				a = rpmArm(ctx, qos, ep, o.Seconds, t0)
				doneSec += float64(o.Seconds + 6)
				L.set(func(l *live) { l.Progress = doneSec / total })
			} else {
				L.set(func(l *live) { l.Phase = sc.Name + ": " + label + " (settling)" })
				// settle with load so the queue (or the shaper) reaches steady state
				measure(ctx, ep, sc.Down, sc.Up, 6*time.Second, t0, nil, nil)
				doneSec += 6
				L.set(func(l *live) { l.Phase = sc.Name + ": " + label; l.Progress = doneSec / total })
				a = arm{QoS: qos, Started: time.Now()}
				rc := &rpmCollector{small: ep.Small}
				base := doneSec
				started := a.Started
				a.Samples, a.DownMbps, a.UpMbps = measure(ctx, ep, sc.Down, sc.Up, time.Duration(o.Seconds)*time.Second, t0, func(s sample) {
					L.set(func(l *live) {
						l.Recent = append(l.Recent, s)
						if len(l.Recent) > 600 {
							l.Recent = l.Recent[len(l.Recent)-600:]
						}
						l.Progress = (base + math.Min(float64(o.Seconds), time.Since(started).Seconds())) / total
					})
				}, rc)
				doneSec += float64(o.Seconds)
				stats(&a)
				if rr := rc.result(); rr != nil {
					rr.Note += "; probes on this scenario's own load, not the draft's connection ramp"
					a.RPM = rr
				}
			}
			if qos == "off" {
				r.Off = a
			} else {
				r.On = a
			}
		}
		res = append(res, r)
		L.set(func(l *live) { l.Results = append(l.Results, summarize(r)) })
	}
	return res, nil
}

func summarize(r scenarioResult) map[string]any {
	if r.Home != nil {
		on := r.Home.Error
		if r.Home.Capacity != nil || r.Home.RPM != nil {
			on = ""
			if r.Home.Capacity != nil {
				on = fmt.Sprintf("%.0f Mbit/s", r.Home.Capacity.MaxMbps)
			}
			if r.Home.RPM != nil {
				on = strings.TrimSpace(on + fmt.Sprintf("  RPM %.0f", r.Home.RPM.RPM))
			}
		}
		return map[string]any{"name": r.Name, "metric": "capacity and responsiveness to the Operator Console", "off": "—",
			"on": on, "change": "inside the home"}
	}
	ch := "—"
	if r.Off.P99 > 0 {
		ch = fmt.Sprintf("%+.0f %%", (r.On.P99-r.Off.P99)/r.Off.P99*100)
	}
	m := map[string]any{"name": r.Name, "metric": "delay p99 (ms)", "off": r.Off.P99, "on": r.On.P99, "change": ch,
		"off_p50": r.Off.P50, "on_p50": r.On.P50, "off_tput": tput(r.Off), "on_tput": tput(r.On)}
	if r.Off.RPM != nil && r.On.RPM != nil {
		m["off_rpm"], m["on_rpm"] = r.Off.RPM.RPM, r.On.RPM.RPM
		if r.ID == "rpm" || r.ID == "home_network" {
			m["metric"], m["off"], m["on"] = "responsiveness (RPM, higher is better)", r.Off.RPM.RPM, r.On.RPM.RPM
			if r.Off.RPM.RPM > 0 {
				m["change"] = fmt.Sprintf("%+.0f %%", (r.On.RPM.RPM-r.Off.RPM.RPM)/r.Off.RPM.RPM*100)
			}
		}
	}
	if r.Off.Quality != nil && r.On.Quality != nil && len(r.Off.Quality.QoO) > 0 {
		m["off_call_qoo"], m["on_call_qoo"] = r.Off.Quality.QoO[0].QoO, r.On.Quality.QoO[0].QoO
		m["off_game_qoo"], m["on_game_qoo"] = r.Off.Quality.QoO[2].QoO, r.On.Quality.QoO[2].QoO
	}
	return m
}

func tput(a arm) string {
	var parts []string
	if a.DownMbps > 0.5 {
		parts = append(parts, fmt.Sprintf("↓ %.1f", a.DownMbps))
	}
	if a.UpMbps > 0.5 {
		parts = append(parts, fmt.Sprintf("↑ %.1f", a.UpMbps))
	}
	return strings.Join(parts, "  ") + " Mbit/s"
}

// ------------------------------------------------------------------ failover (guided)

type foRun struct {
	QoS     string   `json:"qos"`
	Samples []sample `json:"samples"`
	SwitchT float64  `json:"switch_t"`
	Before  arm      `json:"before"`
	After   arm      `json:"after"`
}

var foSignal = make(chan struct{}, 1)

func runFailover(ctx context.Context, rt *router, qos string, ep endpoints, before, after time.Duration, auto bool) (foRun, error) {
	if qos == "off" {
		if err := rt.bypass(true, (before+after).Minutes()+1); err != nil {
			return foRun{}, err
		}
	} else if err := rt.bypass(false, 0); err != nil {
		return foRun{}, err
	}
	defer func() { _ = rt.bypass(false, 0) }()
	t0 := time.Now()
	run := foRun{QoS: qos}
	L.set(func(l *live) {
		l.Phase = "Failover test (" + qos + "): on the fibre line"
		l.Recent = nil
		l.Waiting = ""
	})
	onS := func(s sample) {
		L.set(func(l *live) {
			l.Recent = append(l.Recent, s)
			if len(l.Recent) > 1500 {
				l.Recent = l.Recent[1:]
			}
		})
	}
	s1, d1, u1 := measure(ctx, ep, 6, 4, before, t0, onS, nil)
	run.Before = arm{QoS: qos, Samples: s1, DownMbps: d1, UpMbps: u1}
	stats(&run.Before)
	L.set(func(l *live) {
		l.Phase = "Failover test: move the cable now"
		l.Waiting = "Move the router's WAN cable from the ISP router to the 4G Bridge laptop now, then press “Cable moved”."
	})
	if !auto {
		select {
		case <-foSignal:
		case <-ctx.Done():
			return run, ctx.Err()
		case <-time.After(5 * time.Minute):
			return run, errors.New("the cable was not moved within 5 minutes")
		}
	}
	run.SwitchT = time.Since(t0).Seconds()
	L.set(func(l *live) { l.Waiting = ""; l.Phase = "Failover test (" + qos + "): on 4G, measuring recovery" })
	s2, d2, u2 := measure(ctx, ep, 6, 4, after, t0, onS, nil)
	run.After = arm{QoS: qos, Samples: s2, DownMbps: d2, UpMbps: u2}
	stats(&run.After)
	run.Samples = append(s1, s2...)
	return run, nil
}

// ------------------------------------------------------------------ report

type report struct {
	Title     string           `json:"title"`
	Router    string           `json:"router"`
	Machine   string           `json:"machine"`
	Started   time.Time        `json:"started"`
	Results   []scenarioResult `json:"results,omitempty"`
	Failover  []foRun          `json:"failover,omitempty"`
	Scenarios []map[string]any `json:"scenarios"`
	Summary   string           `json:"summary"`
	Verdict   string           `json:"verdict"`
	Note      string           `json:"note"`
	Version   string           `json:"version"`
}

func buildReport(rt string, res []scenarioResult, fo []foRun, started time.Time) report {
	r := report{Router: rt, Machine: machineName(), Started: started, Results: res, Failover: fo, Version: version,
		Note: "Delay = reply time of small UDP packets (DNS queries to the public resolvers 1.1.1.1, 8.8.8.8 and 9.9.9.9, " +
			"10 a second in a steady stream, like a call or a game) while the load runs. It includes every queue between this " +
			"laptop and the internet, which is what calls and games feel. " +
			"'IntentQoS off' uses the router's no-QoS test window (shaping removed, restored automatically)."}
	better, worse := 0, 0
	for _, x := range res {
		s := summarize(x)
		r.Scenarios = append(r.Scenarios, s)
		if x.On.P99 < x.Off.P99*0.8 {
			better++
		} else if x.On.P99 > x.Off.P99*1.2 {
			worse++
		}
	}
	for _, f := range fo {
		r.Scenarios = append(r.Scenarios, map[string]any{"name": "Fibre → 4G failover (" + f.QoS + ")", "metric": "delay p99 after the switch (ms)",
			"off": f.Before.P99, "on": f.After.P99, "change": "before → after"})
	}
	switch {
	case len(res) == 0:
		r.Verdict = "info"
	case worse > 0:
		r.Verdict = "worse"
	case better > 0:
		r.Verdict = "better"
	default:
		r.Verdict = "same"
	}
	if len(res) > 0 {
		x := res[0]
		r.Title = fmt.Sprintf("Line test: %d scenario(s), %s", len(res), started.Format("2 Jan 15:04"))
		r.Summary = fmt.Sprintf("%s: delay p99 %.0f → %.0f ms", x.Name, x.Off.P99, x.On.P99)
	} else {
		r.Title = "Failover test, " + started.Format("2 Jan 15:04")
		if len(fo) > 0 {
			r.Summary = fmt.Sprintf("after the switch: p99 %.0f ms (%s)", fo[0].After.P99, fo[0].QoS)
		}
	}
	return r
}

func writeReport(rep report) (string, string, error) {
	dir := reportsDir()
	stamp := rep.Started.Format("2006-01-02_1504")
	jp := filepath.Join(dir, "IntentQoS-test-"+stamp+".json")
	hp := filepath.Join(dir, "IntentQoS-test-"+stamp+".html")
	b, _ := json.MarshalIndent(rep, "", " ")
	if err := os.WriteFile(jp, b, 0o644); err != nil {
		return "", "", err
	}
	t := template.Must(template.New("r").Funcs(template.FuncMap{"chart": chartSVG, "pct": func(f float64) string { return fmt.Sprintf("%.1f", f) }}).
		Parse(string(webFile("report.html"))))
	f, err := os.Create(hp)
	if err != nil {
		return "", "", err
	}
	defer f.Close()
	if err := t.Execute(f, rep); err != nil {
		return "", "", err
	}
	return hp, jp, nil
}

func reportsDir() string {
	h, _ := os.UserHomeDir()
	d := filepath.Join(h, "Documents", "IntentQoS Reports")
	if os.MkdirAll(d, 0o755) != nil {
		d = dataDir("reports")
	}
	return d
}

// chartSVG draws delay over time for QoS off (grey) and on (green) as an inline SVG.
func chartSVG(off, on []sample, mark float64) template.HTML {
	const W, H, pad = 760.0, 220.0, 36.0
	maxT, maxY := 1.0, 10.0
	for _, s := range append(append([]sample{}, off...), on...) {
		if s.RTT > maxY {
			maxY = s.RTT
		}
	}
	norm := func(ss []sample) []sample {
		if len(ss) == 0 {
			return ss
		}
		t0 := ss[0].T
		out := make([]sample, len(ss))
		for i, s := range ss {
			out[i] = sample{T: s.T - t0, RTT: s.RTT}
			if out[i].T > maxT {
				maxT = out[i].T
			}
		}
		return out
	}
	if mark > 0 {
		off = norm(off) // failover: one continuous run, drawn as "off" (before) and marked at the switch
		maxT = 0
		for _, s := range off {
			if s.T > maxT {
				maxT = s.T
			}
		}
	} else {
		off, on = norm(off), norm(on)
	}
	maxY = math.Ceil(maxY/10) * 10
	x := func(t float64) float64 { return pad + t/maxT*(W-pad-8) }
	y := func(v float64) float64 { return H - 22 - v/maxY*(H-36) }
	var b strings.Builder
	fmt.Fprintf(&b, `<svg viewBox="0 0 %.0f %.0f" role="img" style="width:100%%;height:auto">`, W, H)
	for i := 0; i <= 4; i++ {
		v := maxY * float64(i) / 4
		fmt.Fprintf(&b, `<line x1="%.0f" x2="%.0f" y1="%.1f" y2="%.1f" stroke="#D8DCE3"/><text x="%.0f" y="%.1f" font-size="10" fill="#576173" text-anchor="end">%.0f</text>`,
			pad, W-8, y(v), y(v), pad-4, y(v)+3, v)
	}
	fmt.Fprintf(&b, `<text x="%.0f" y="%.0f" font-size="10" fill="#576173">ms</text><text x="%.0f" y="%.0f" font-size="10" fill="#576173" text-anchor="end">%.0f s</text>`, 4.0, 12.0, W-8, H-6, maxT)
	dots := func(ss []sample, col string) {
		for _, s := range ss {
			if s.RTT < 0 {
				fmt.Fprintf(&b, `<text x="%.1f" y="%.1f" font-size="9" fill="#C0321D">×</text>`, x(s.T), y(maxY)+4)
				continue
			}
			fmt.Fprintf(&b, `<circle cx="%.1f" cy="%.1f" r="1.6" fill="%s"/>`, x(s.T), y(s.RTT), col)
		}
	}
	dots(off, "#9AA5B1")
	dots(on, "#1C7C52")
	if mark > 0 {
		fmt.Fprintf(&b, `<line x1="%.1f" x2="%.1f" y1="14" y2="%.0f" stroke="#C25A0C" stroke-dasharray="4 3"/><text x="%.1f" y="12" font-size="10" fill="#C25A0C">cable moved to 4G</text>`,
			x(mark), x(mark), H-22, x(mark)+4)
	}
	b.WriteString(`</svg>`)
	return template.HTML(b.String())
}

func uploadReport(rep report) (string, error) {
	prof := loadProfile()
	if prof.Console == nil || prof.TestToken == "" {
		return "", errors.New("not paired with an Operator Console")
	}
	var out struct {
		ID string `json:"id"`
	}
	if err := callJSON(consoleClient(prof.ConsoleCA), "POST", strings.TrimRight(prof.Console.HTTPS, "/")+"/v1/kit/results", prof.TestToken, rep, &out); err != nil {
		return "", err
	}
	return out.ID, nil
}

func openFile(p string) {
	switch runtime.GOOS {
	case "windows":
		_ = exec.Command("rundll32", "url.dll,FileProtocolHandler", p).Start()
	case "darwin":
		_ = exec.Command("open", p).Start()
	default:
		_ = exec.Command("xdg-open", p).Start()
	}
}

// ------------------------------------------------------------------ pairing

func pairTestkit(consoleAddr, pin string) (*Profile, error) {
	var ci *ConsoleInfo
	var err error
	if consoleAddr == "" || strings.HasPrefix(consoleAddr, "https://") {
		for _, c := range discoverConsoles(1500 * time.Millisecond) {
			if consoleAddr == "" || strings.TrimRight(c.HTTPS, "/") == strings.TrimRight(consoleAddr, "/") {
				cc := c
				ci = &cc
				break
			}
		}
	}
	if ci == nil {
		if consoleAddr == "" {
			return nil, errors.New("no Operator Console found on this network: start it, or type its address")
		}
		if ci, err = consoleAt(consoleAddr); err != nil {
			return nil, err
		}
	}
	var r struct {
		Token   string       `json:"token"`
		Routers []RouterInfo `json:"routers"`
		CA      string       `json:"ca_pem"`
	}
	body := map[string]any{"pin": strings.ReplaceAll(pin, " ", ""), "role": "testkit", "name": "Test Kit on " + machineName(), "machine": machineName()}
	if err := callJSON(consoleClient(""), "POST", strings.TrimRight(ci.HTTPS, "/")+"/v1/kit/pair", "", body, &r); err != nil {
		return nil, fmt.Errorf("pairing failed: %v", err)
	}
	prof := loadProfile()
	prof.Console, prof.TestToken, prof.ConsoleCA = ci, r.Token, r.CA
	for _, rt := range r.Routers {
		prof.upsertRouter(rt)
	}
	return prof, prof.save()
}

// ------------------------------------------------------------------ program

func runPart(args []string) {
	fs := flag.NewFlagSet("testkit", flag.ExitOnError)
	rip := fs.String("router", "", "command-line mode: router address")
	key := fs.String("key", "", "router key (shown by Router Setup); default: from this laptop's kit profile")
	pin := fs.String("pin", "", "pair with the Operator Console using this PIN (gets the router and its key)")
	console := fs.String("console", "", "Operator Console address (default: find it)")
	secs := fs.Int("seconds", 30, "seconds per measurement")
	scen := fs.String("scenarios", "call_backup,game_downloads", "tests to run: "+scenarioIDs())
	failover := fs.Bool("failover", false, "guided failover test instead (press Enter when the cable is moved)")
	upload := fs.Bool("upload", true, "send the report to the Operator Console when paired")
	cli := fs.Bool("cli", false, "command-line mode with the router saved on this laptop")
	_ = fs.Parse(args)
	setupLog("testkit")
	if *cli || *pin != "" || *rip != "" || *failover {
		os.Exit(cliTestkit(*rip, *key, *pin, *console, *secs, *scen, *failover, *upload))
	}
	app := newApp()
	var runMu sync.Mutex
	var cancel context.CancelFunc
	app.Mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write(webFile("testkit.html"))
	})
	app.Mux.HandleFunc("/api/profile", func(w http.ResponseWriter, r *http.Request) {
		p := loadProfile()
		writeJSON(w, 200, map[string]any{"console": p.Console, "paired": p.TestToken != "", "routers": p.Routers,
			"scenarios": scenarios, "consoles": discoverConsoles(1200 * time.Millisecond), "gateways": defaultGateways()})
	})
	app.Mux.HandleFunc("/api/pair", func(w http.ResponseWriter, r *http.Request) {
		var q struct{ Console, PIN string }
		if err := readJSON(r, &q); err != nil {
			errJSON(w, err)
			return
		}
		p, err := pairTestkit(q.Console, q.PIN)
		if err != nil {
			errJSON(w, err)
			return
		}
		writeJSON(w, 200, map[string]any{"routers": p.Routers, "console": p.Console})
	})
	app.Mux.HandleFunc("/api/check", func(w http.ResponseWriter, r *http.Request) {
		var q struct{ Router, Key string }
		if err := readJSON(r, &q); err != nil {
			errJSON(w, err)
			return
		}
		var st map[string]any
		if err := newRouter(q.Router, q.Key).call("GET", "/api/state", nil, &st); err != nil {
			errJSON(w, fmt.Errorf("the router at %s did not accept that key, or IntentQoS is not running there: %v", q.Router, err))
			return
		}
		p := loadProfile()
		p.upsertRouter(RouterInfo{Serial: "router-" + q.Router, LanIP: q.Router, Key: q.Key, Console: "http://" + q.Router + ":8080/"})
		_ = p.save()
		writeJSON(w, 200, map[string]any{"ok": true, "config": st["config"], "estimator": st["estimator"], "bypass": st["bypass"]})
	})
	start := func(w http.ResponseWriter, f func(ctx context.Context) error) {
		runMu.Lock()
		defer runMu.Unlock()
		if L.Running {
			errJSON(w, errors.New("a test is already running"))
			return
		}
		ctx, c := context.WithCancel(context.Background())
		cancel = c
		L.set(func(l *live) { // reset every field but the mutex
			l.Running, l.Phase, l.Progress, l.Recent, l.DownMbps, l.UpMbps = true, "Starting", 0, nil, 0, 0
			l.Results, l.Error, l.Report, l.Uploaded, l.Waiting, l.Done = nil, "", "", "", "", false
		})
		go func() {
			err := f(ctx)
			L.set(func(l *live) {
				l.Running, l.Done = false, true
				if err != nil && !errors.Is(err, context.Canceled) {
					l.Error = err.Error()
				}
			})
		}()
		writeJSON(w, 200, map[string]any{"ok": true})
	}
	app.Mux.HandleFunc("/api/run", func(w http.ResponseWriter, r *http.Request) {
		var o testOpts
		if err := readJSON(r, &o); err != nil {
			errJSON(w, err)
			return
		}
		start(w, func(ctx context.Context) error {
			t := time.Now()
			res, err := runTest(ctx, o, defaultEndpoints())
			if err != nil {
				return err
			}
			return finish(buildReport(o.Router, res, nil, t))
		})
	})
	app.Mux.HandleFunc("/api/failover", func(w http.ResponseWriter, r *http.Request) {
		var o struct {
			Router, Key, QoS string
		}
		if err := readJSON(r, &o); err != nil {
			errJSON(w, err)
			return
		}
		start(w, func(ctx context.Context) error {
			t := time.Now()
			run, err := runFailover(ctx, newRouter(o.Router, o.Key), o.QoS, defaultEndpoints(), 20*time.Second, 60*time.Second, false)
			if err != nil {
				return err
			}
			return finish(buildReport(o.Router, nil, []foRun{run}, t))
		})
	})
	app.Mux.HandleFunc("/api/moved", func(w http.ResponseWriter, r *http.Request) {
		select {
		case foSignal <- struct{}{}:
		default:
		}
		writeJSON(w, 200, map[string]any{"ok": true})
	})
	app.Mux.HandleFunc("/api/stop", func(w http.ResponseWriter, r *http.Request) {
		runMu.Lock()
		if cancel != nil {
			cancel()
		}
		runMu.Unlock()
		writeJSON(w, 200, map[string]any{"ok": true})
	})
	app.Mux.HandleFunc("/api/live", func(w http.ResponseWriter, r *http.Request) {
		L.mu.Lock()
		defer L.mu.Unlock()
		writeJSON(w, 200, L)
	})
	app.Mux.HandleFunc("/api/open-report", func(w http.ResponseWriter, r *http.Request) {
		L.mu.Lock()
		p := L.Report
		L.mu.Unlock()
		if p != "" {
			openFile(p)
		}
		writeJSON(w, 200, map[string]any{"ok": p != ""})
	})
	url, err := app.Serve()
	if err != nil {
		msgBox(partTitle, err.Error())
		return
	}
	app.KeepRun = func() bool { L.mu.Lock(); defer L.mu.Unlock(); return L.Running }
	openWindow(url, "testkit", 1000, 900)
	app.WaitClosed(20 * time.Second)
	runMu.Lock()
	if cancel != nil {
		cancel()
	}
	runMu.Unlock()
	time.Sleep(time.Second)
}

func finish(rep report) error {
	hp, _, err := writeReport(rep)
	if err != nil {
		return err
	}
	id, uerr := uploadReport(rep)
	L.set(func(l *live) {
		l.Report = hp
		if uerr == nil {
			l.Uploaded = id
		}
		l.Phase = "Finished"
		l.Progress = 1
	})
	logf("report: %s (console: %v %v)", hp, id, uerr)
	return nil
}

func scenarioIDs() string {
	var ids []string
	for _, s := range scenarios {
		ids = append(ids, s.ID)
	}
	return strings.Join(ids, ",")
}

func cliTestkit(rip, key, pin, console string, secs int, scen string, failover, upload bool) int {
	if pin != "" {
		p, err := pairTestkit(console, pin)
		if err != nil {
			fmt.Fprintln(os.Stderr, "error:", err)
			return 2
		}
		fmt.Printf("paired with the Operator Console at %s; routers: %d\n", p.Console.IP, len(p.Routers))
	}
	prof := loadProfile()
	if rip == "" && len(prof.Routers) > 0 {
		rip = prof.Routers[len(prof.Routers)-1].LanIP
	}
	if key == "" {
		for _, r := range prof.Routers {
			if r.LanIP == rip && r.Key != "" {
				key = r.Key
			}
		}
	}
	if rip == "" || key == "" {
		fmt.Fprintln(os.Stderr, "error: give --router and --key, or pair with --pin")
		return 2
	}
	t := time.Now()
	ctx := context.Background()
	var rep report
	if failover {
		go func() {
			fmt.Println("When asked, move the router's WAN cable to the 4G Bridge laptop and press Enter.")
			buf := make([]byte, 1)
			for {
				if _, err := os.Stdin.Read(buf); err != nil {
					return
				}
				if buf[0] == '\n' {
					foSignal <- struct{}{}
				}
			}
		}()
		auto := os.Getenv("INTENTQOS_FAILOVER_AUTO") != "" // tests: the line is switched by a script at a known time
		run, err := runFailover(ctx, newRouter(rip, key), "on", defaultEndpoints(), time.Duration(secs)*time.Second, time.Duration(2*secs)*time.Second, auto)
		if err != nil {
			fmt.Fprintln(os.Stderr, "error:", err)
			return 1
		}
		rep = buildReport(rip, nil, []foRun{run}, t)
	} else {
		go func() {
			last := ""
			for {
				time.Sleep(2 * time.Second)
				L.mu.Lock()
				ph, pr := L.Phase, L.Progress
				L.mu.Unlock()
				if ph != last {
					fmt.Printf("  [%3.0f%%] %s\n", pr*100, ph)
					last = ph
				}
			}
		}()
		res, err := runTest(ctx, testOpts{Scenarios: strings.Split(scen, ","), Seconds: secs, Router: rip, Key: key}, defaultEndpoints())
		if err != nil {
			fmt.Fprintln(os.Stderr, "error:", err)
			return 1
		}
		rep = buildReport(rip, res, nil, t)
	}
	hp, jp, err := writeReport(rep)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return 1
	}
	for _, s := range rep.Scenarios {
		fmt.Printf("%-40s %-32s off/before %6v   on/after %6v   %v\n", s["name"], s["metric"], s["off"], s["on"], s["change"])
	}
	fmt.Println("report:", hp, "\ndata:  ", jp)
	if upload {
		if id, err := uploadReport(rep); err == nil {
			fmt.Println("sent to the Operator Console:", id)
		} else {
			fmt.Println("not sent to a console:", err)
		}
	}
	return 0
}
