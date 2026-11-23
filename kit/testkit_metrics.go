//go:build testkit

// What the measured delays mean for people (Quality of Outcome), how they split (ΔQ), a percentile summary
// in the shape TRAI's 2024 regulations use, and RPM (responsiveness under working conditions).
package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"math"
	"math/rand"
	"net"
	"net/http"
	"net/url"
	"sort"
	"sync"
	"time"
)

// ------------------------------------------------------------------ Quality of Outcome (IETF draft-ietf-ippm-qoo)

// Same requirement values as the router engine (intentqos/qoo.py): percentile -> (ROP, CPUP) ms, loss %.
type qooReq struct {
	Name string
	Lat  map[int][2]float64
	Loss [2]float64
}

var qooReqs = []qooReq{
	{"Video call", map[int][2]float64{50: {80, 350}, 99: {200, 600}}, [2]float64{1.0, 8.0}},
	{"Voice call", map[int][2]float64{50: {60, 300}, 99: {150, 500}}, [2]float64{0.5, 5.0}},
	{"Online game", map[int][2]float64{50: {30, 150}, 99: {60, 250}}, [2]float64{0.5, 3.0}},
	{"Web browsing", map[int][2]float64{50: {100, 600}, 99: {300, 2000}}, [2]float64{1.0, 5.0}},
}

func interp(m, rop, cpup float64) float64 {
	if rop == cpup {
		if m <= rop {
			return 100
		}
		return 0
	}
	return math.Max(0, math.Min(100, 100*(cpup-m)/(cpup-rop)))
}

type qooScore struct {
	App   string  `json:"app"`
	QoO   float64 `json:"qoo"`
	Worst string  `json:"worst"`
}

func qooOf(r qooReq, p50, p99, loss float64) qooScore {
	parts := map[string]float64{"p50": interp(p50, r.Lat[50][0], r.Lat[50][1]), "p99": interp(p99, r.Lat[99][0], r.Lat[99][1]),
		"loss": interp(loss, r.Loss[0], r.Loss[1])}
	s := qooScore{App: r.Name, QoO: 101}
	for _, k := range []string{"p50", "p99", "loss"} {
		if parts[k] < s.QoO {
			s.QoO, s.Worst = parts[k], k
		}
	}
	s.QoO = math.Round(s.QoO*10) / 10
	return s
}

// ------------------------------------------------------------------ the summary of one arm

type quality struct {
	QoO []qooScore `json:"qoo"`
	// ΔQ: G = the floor (minimum round trip: distance and routing), V = what queues add above it
	G   float64 `json:"g_ms"`
	V50 float64 `json:"v50_ms"`
	V99 float64 `json:"v99_ms"`
	// jitter as the mean and 90th percentile of the change between consecutive round trips (RFC 3393 IPDV)
	JitterMean float64 `json:"jitter_mean_ms"`
	JitterP90  float64 `json:"jitter_p90_ms"`
	P90        float64 `json:"p90_ms"`
	P95        float64 `json:"p95_ms"`
	// TRAI-style: percentiles (not averages) next to the 2024 wireline reference values
	TRAI map[string]any `json:"trai_reference"`
}

func pct(v []float64, p float64) float64 {
	if len(v) == 0 {
		return 0
	}
	return v[int(math.Min(float64(len(v)-1), math.Floor(p*float64(len(v)-1)+0.5)))]
}

func assess(a *arm) {
	var v, jit []float64
	prev := -1.0
	for _, s := range a.Samples {
		if s.RTT < 0 {
			prev = -1
			continue
		}
		v = append(v, s.RTT)
		if prev >= 0 {
			jit = append(jit, math.Abs(s.RTT-prev))
		}
		prev = s.RTT
	}
	if len(v) == 0 {
		return
	}
	sort.Float64s(v)
	sort.Float64s(jit)
	r1 := func(x float64) float64 { return math.Round(x*10) / 10 }
	q := &quality{G: r1(v[0]), V50: r1(pct(v, 0.5) - v[0]), V99: r1(pct(v, 0.99) - v[0]), P90: r1(pct(v, 0.9)), P95: r1(pct(v, 0.95))}
	if len(jit) > 0 {
		s := 0.0
		for _, x := range jit {
			s += x
		}
		q.JitterMean, q.JitterP90 = r1(s/float64(len(jit))), r1(pct(jit, 0.9))
	}
	for _, r := range qooReqs {
		q.QoO = append(q.QoO, qooOf(r, a.P50, a.P99, a.Loss))
	}
	q.TRAI = map[string]any{
		"latency_p90_ms": q.P90, "latency_reference_ms": 50, "latency_within": q.P90 < 50,
		"jitter_p90_ms": q.JitterP90, "jitter_reference_ms": 40, "jitter_within": q.JitterP90 < 40,
		"packet_loss_pct": a.Loss,
		"note": "Reference values: TRAI's 2024 QoS regulations for wireline broadband (latency under 50 ms, jitter under " +
			"40 ms, from 1 Oct 2024, as reported by MediaNama); TRAI also moved to percentile-based reporting. TRAI measures " +
			"the operator's network; this test runs through the home network and the router, so it is a reference, not a " +
			"compliance test."}
	a.Quality = q
}

// ------------------------------------------------------------------ RPM (draft-ietf-ippm-responsiveness-09)

type rpmResult struct {
	RPM        float64 `json:"rpm"`
	Foreign    float64 `json:"foreign_rpm"`
	Loaded     float64 `json:"loaded_rpm"`
	TCPms      float64 `json:"tcp_f_ms"`
	TLSms      float64 `json:"tls_f_ms"`
	HTTPms     float64 `json:"http_f_ms"`
	LoadedMs   float64 `json:"http_l_ms"`
	Probes     int     `json:"probes"`
	Confidence string  `json:"confidence,omitempty"`
	Conns      int     `json:"load_connections,omitempty"`
	GoodputMb  float64 `json:"goodput_mbps,omitempty"`
	Server     string  `json:"server"`
	Note       string  `json:"note"`
}

type rpmCollector struct {
	mu                          sync.Mutex
	tcp, tlsT, httpF, httpL     []float64
	small                       string
	pinned                      string // console lab certificate fingerprint, "" = public server (normal TLS)
}

// trimmed is the single-sided trimmed mean the draft uses (TMP = 95 %: the worst 5 % of samples dropped).
func trimmed(x []float64) float64 {
	if len(x) == 0 {
		return 0
	}
	v := append([]float64(nil), x...)
	sort.Float64s(v)
	k := int(math.Ceil(float64(len(v)) * 0.95))
	if k < 1 {
		k = 1
	}
	s := 0.0
	for _, y := range v[:k] {
		s += y
	}
	return s / float64(k)
}

func (rc *rpmCollector) result() *rpmResult {
	rc.mu.Lock()
	defer rc.mu.Unlock()
	if len(rc.httpL) == 0 || len(rc.httpF) == 0 {
		return nil
	}
	r := &rpmResult{TCPms: trimmed(rc.tcp), TLSms: trimmed(rc.tlsT), HTTPms: trimmed(rc.httpF), LoadedMs: trimmed(rc.httpL),
		Probes: len(rc.httpL), Server: rc.small}
	if r.TLSms > 0 {
		r.Foreign = 60000 / ((r.TCPms + r.TLSms + r.HTTPms) / 3)
	} else {
		r.Foreign = 60000 / ((r.TCPms + r.HTTPms) / 2)
	}
	r.Loaded = 60000 / r.LoadedMs
	r.RPM = math.Round((r.Foreign + r.Loaded) / 2)
	r.Foreign, r.Loaded = math.Round(r.Foreign), math.Round(r.Loaded)
	for _, p := range []*float64{&r.TCPms, &r.TLSms, &r.HTTPms, &r.LoadedMs} {
		*p = math.Round(*p*10) / 10
	}
	r.Note = "draft-ietf-ippm-responsiveness-09 formula: (60000/mean(TM(tcp_f),TM(tls_f),TM(http_f)) + 60000/TM(http_l)) / 2, " +
		"TM = trimmed mean dropping the worst 5 %"
	return r
}

// probe runs one foreign probe (new TCP + TLS connection, then a 1-byte GET) and one loaded probe (a 1-byte GET
// multiplexed onto a load-generating connection).
func (rc *rpmCollector) probe(ctx context.Context, loaded *http.Client) {
	u, err := url.Parse(rc.small)
	if err != nil {
		return
	}
	host := u.Host
	if u.Port() == "" {
		host = net.JoinHostPort(u.Hostname(), "443")
	}
	go func() { // loaded
		t := time.Now()
		req, _ := http.NewRequestWithContext(ctx, "GET", rc.small, nil)
		resp, err := loaded.Do(req)
		if err != nil {
			return
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		ms := float64(time.Since(t).Microseconds()) / 1000
		rc.mu.Lock()
		rc.httpL = append(rc.httpL, ms)
		rc.mu.Unlock()
	}()
	d := net.Dialer{Timeout: 5 * time.Second}
	t0 := time.Now()
	c, err := d.DialContext(ctx, "tcp", host)
	if err != nil {
		return
	}
	defer c.Close()
	tcp := float64(time.Since(t0).Microseconds()) / 1000
	cfg := &tls.Config{ServerName: u.Hostname(), NextProtos: []string{"http/1.1"}}
	if rc.pinned != "" {
		cl := pinnedClient(rc.pinned, 1)[0]
		cfg = cl.Transport.(*http.Transport).TLSClientConfig.Clone()
		cfg.NextProtos = []string{"http/1.1"}
	}
	tc := tls.Client(c, cfg)
	t1 := time.Now()
	_ = tc.SetDeadline(time.Now().Add(5 * time.Second))
	if err := tc.HandshakeContext(ctx); err != nil {
		return
	}
	tlsMs := float64(time.Since(t1).Microseconds()) / 1000
	if tc.ConnectionState().Version < tls.VersionTLS13 {
		tlsMs /= 2 // the draft normalises TLS time to round trips: TLS 1.2 takes two
	}
	t2 := time.Now()
	fmt.Fprintf(tc, "GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: IntentQoS-TestKit\r\nConnection: close\r\n\r\n", u.RequestURI(), u.Host)
	resp, err := http.ReadResponse(bufio.NewReader(tc), nil)
	if err != nil {
		return
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	httpMs := float64(time.Since(t2).Microseconds()) / 1000
	rc.mu.Lock()
	rc.tcp, rc.tlsT, rc.httpF = append(rc.tcp, tcp), append(rc.tlsT, tlsMs), append(rc.httpF, httpMs)
	rc.mu.Unlock()
}

// run sends probe pairs at `perSec` until ctx ends (the draft allows up to MPS = 100 a second; the Test Kit
// uses fewer so the probes stay well under the 5 % capacity limit on slow lines).
func (rc *rpmCollector) run(ctx context.Context, loaded []*http.Client, perSec int) {
	tick := time.NewTicker(time.Second / time.Duration(perSec))
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			go rc.probe(ctx, loaded[rand.Intn(len(loaded))])
		}
	}
}

// runRPM follows the draft's procedure for one direction: load-generating connections start at 1 and grow by
// 1 every 5-second interval (up to 16) until goodput is stable (standard deviation of the last 4 moving averages
// within 5 %), then responsiveness is reported when it is stable the same way, or when the time limit is hit
// (confidence Low / Medium / High as the draft defines).
func runRPM(ctx context.Context, large, small, upload, pinned string, uplink bool, limit time.Duration,
	onProgress func(string)) *rpmResult {
	const ID, MAD, MNP, SDT = 5 * time.Second, 4, 16, 0.05
	ctx, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	newClient := func() *http.Client {
		if pinned != "" {
			return pinnedClient(pinned, 1)[0]
		}
		return &http.Client{Transport: &http.Transport{Proxy: http.ProxyFromEnvironment, ForceAttemptHTTP2: true, MaxConnsPerHost: 1}}
	}
	var conns []*http.Client
	var bytes int64
	var bmu sync.Mutex
	addConn := func() {
		c := newClient()
		conns = append(conns, c)
		go func() {
			buf := make([]byte, 64<<10)
			for ctx.Err() == nil {
				var resp *http.Response
				var err error
				if uplink {
					cnt := &countReader{z: zeroBody{left: 1 << 30}, ctx: ctx, add: func(n int) { bmu.Lock(); bytes += int64(n); bmu.Unlock() }}
					req, _ := http.NewRequestWithContext(ctx, "POST", upload, cnt)
					resp, err = c.Do(req)
				} else {
					req, _ := http.NewRequestWithContext(ctx, "GET", large, nil)
					if resp, err = c.Do(req); err == nil {
						for {
							n, e := resp.Body.Read(buf)
							bmu.Lock()
							bytes += int64(n)
							bmu.Unlock()
							if e != nil {
								break
							}
						}
					}
				}
				if err != nil {
					sleepCtx(ctx, 300*time.Millisecond)
					continue
				}
				_, _ = io.Copy(io.Discard, resp.Body)
				resp.Body.Close()
			}
		}()
	}
	addConn()
	var gp, rpmHist, gpMA []float64
	var last *rpmResult
	saturated, stable := false, false
	intervals := 0
	for ctx.Err() == nil && !stable {
		rc := &rpmCollector{small: small, pinned: pinned}
		ictx, icancel := context.WithTimeout(ctx, ID)
		bmu.Lock()
		b0 := bytes
		bmu.Unlock()
		rc.run(ictx, conns, 20)
		icancel()
		if ctx.Err() != nil && intervals > 0 {
			break
		}
		intervals++
		bmu.Lock()
		gp = append(gp, float64(bytes-b0)*8/1e6/ID.Seconds())
		bmu.Unlock()
		gpMA = append(gpMA, mean(lastN(gp, MAD)))
		if r := rc.result(); r != nil {
			last = r
			rpmHist = append(rpmHist, r.RPM)
		}
		if !saturated {
			saturated = len(gpMA) >= MAD && stddev(lastN(gpMA, MAD)) <= SDT*gpMA[len(gpMA)-1]
			if !saturated && len(conns) < MNP {
				addConn()
			}
		} else if len(rpmHist) >= MAD {
			w := lastN(rpmHist, MAD)
			stable = stddev(w) <= SDT*w[len(w)-1]
		}
		if onProgress != nil && last != nil {
			onProgress(fmt.Sprintf("RPM %0.f with %d connections, %.0f Mbit/s", last.RPM, len(conns), gpMA[len(gpMA)-1]))
		}
	}
	if last == nil {
		return nil
	}
	last.Conns = len(conns)
	last.GoodputMb = math.Round(gpMA[len(gpMA)-1]*10) / 10
	switch {
	case stable:
		last.Confidence = "High"
	case intervals >= MAD:
		last.Confidence = "Medium"
	default:
		last.Confidence = "Low"
	}
	last.Note += fmt.Sprintf("; %s load, %d connections, intervals of 5 s, stability over the last 4", map[bool]string{true: "upload", false: "download"}[uplink], len(conns))
	return last
}

type countReader struct {
	z   zeroBody
	ctx context.Context
	add func(int)
}

func (c *countReader) Read(p []byte) (int, error) {
	if c.ctx.Err() != nil {
		return 0, c.ctx.Err()
	}
	n, err := c.z.Read(p)
	c.add(n)
	return n, err
}

func lastN(x []float64, n int) []float64 {
	if len(x) <= n {
		return x
	}
	return x[len(x)-n:]
}

func mean(x []float64) float64 {
	if len(x) == 0 {
		return 0
	}
	s := 0.0
	for _, v := range x {
		s += v
	}
	return s / float64(len(x))
}

func stddev(x []float64) float64 {
	m := mean(x)
	s := 0.0
	for _, v := range x {
		s += (v - m) * (v - m)
	}
	return math.Sqrt(s / float64(len(x)))
}
