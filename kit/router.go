//go:build router

// IntentQoS Router Setup: installs IntentQoS on an OpenWrt / GL.iNet router over SSH, configures it for that
// router automatically, and pairs it with the Operator Console. Also updates and uninstalls.
package main

import (
	"bufio"
	"bytes"
	_ "embed"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"golang.org/x/crypto/ssh"
)

//go:embed embed/router-payload.tar.gz
var routerPayload []byte

const partTitle = "IntentQoS Router Setup"

// ------------------------------------------------------------------ SSH

type routerConn struct {
	mu     sync.Mutex
	client *ssh.Client
	host   string
	facts  map[string]string
}

var rc routerConn

func sshDial(host, user, pass string, trustNew bool) (*ssh.Client, string, error) {
	prof := loadProfile()
	var seenFP string
	cb := func(hostname string, remote net.Addr, key ssh.PublicKey) error {
		fp := fingerprint(key.Marshal())
		seenFP = fp
		known := prof.KnownHosts[host]
		if known != "" && known != fp && !trustNew {
			return fmt.Errorf("HOSTKEY_CHANGED")
		}
		return nil
	}
	answer := func(name, instr string, qs []string, echos []bool) ([]string, error) {
		a := make([]string, len(qs))
		for i := range a {
			a[i] = pass
		}
		return a, nil
	}
	cfg := &ssh.ClientConfig{User: user, HostKeyCallback: cb, Timeout: 10 * time.Second,
		Auth: []ssh.AuthMethod{ssh.Password(pass), ssh.KeyboardInteractive(answer)}}
	addr := host
	if !strings.Contains(addr, ":") {
		addr += ":22"
	}
	c, err := ssh.Dial("tcp", addr, cfg)
	if err != nil {
		if strings.Contains(err.Error(), "HOSTKEY_CHANGED") {
			return nil, seenFP, errors.New("this router's identity (SSH host key) is different from last time. If you reset or " +
				"reflashed the router this is expected: tick 'trust the new router identity' and connect again")
		}
		if strings.Contains(err.Error(), "unable to authenticate") {
			return nil, seenFP, errors.New("the router did not accept that password. GL.iNet: use the admin panel password. " +
				"OpenWrt: the root password set in LuCI (leave it empty if you never set one)")
		}
		return nil, seenFP, fmt.Errorf("could not reach the router's SSH service at %s: %v", addr, err)
	}
	prof.KnownHosts[host] = seenFP
	_ = prof.save()
	return c, seenFP, nil
}

func sshRun(c *ssh.Client, cmd string) (string, error) {
	s, err := c.NewSession()
	if err != nil {
		return "", err
	}
	defer s.Close()
	out, err := s.CombinedOutput(cmd)
	return string(out), err
}

func sshStream(c *ssh.Client, cmd string, line func(string)) error {
	s, err := c.NewSession()
	if err != nil {
		return err
	}
	defer s.Close()
	pr, pw := io.Pipe()
	s.Stdout, s.Stderr = pw, pw
	done := make(chan struct{})
	go func() {
		sc := bufio.NewScanner(pr)
		sc.Buffer(make([]byte, 64*1024), 1<<20)
		for sc.Scan() {
			line(sc.Text())
		}
		close(done)
	}()
	err = s.Run(cmd)
	pw.Close()
	<-done
	return err
}

func sshUpload(c *ssh.Client, data []byte, path string) error {
	s, err := c.NewSession()
	if err != nil {
		return err
	}
	defer s.Close()
	s.Stdin = bytes.NewReader(data)
	out, err := s.CombinedOutput("cat > " + shq(path))
	if err != nil {
		return fmt.Errorf("upload failed: %v %s", err, out)
	}
	return nil
}

func shq(s string) string { return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'" }

// routerFacts reads what the router is, from files every OpenWrt has.
func routerFacts(c *ssh.Client) map[string]string {
	script := `. /etc/openwrt_release 2>/dev/null
echo "release=$DISTRIB_RELEASE"; echo "description=$DISTRIB_DESCRIPTION"
M=$(cat /tmp/sysinfo/model 2>/dev/null); [ -n "$M" ] || M=$(ubus call system board 2>/dev/null | jsonfilter -e '@.model' 2>/dev/null)
[ -n "$M" ] || M=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null); echo "model=$M"
echo "gl=$(cat /etc/glversion 2>/dev/null)"
echo "machine=$(uname -m)"
echo "pm=$(command -v apk >/dev/null 2>&1 && [ -d /etc/apk ] && echo apk || (command -v opkg >/dev/null && echo opkg))"
echo "free_kb=$(df -k /overlay 2>/dev/null | awk 'NR==2 {print $4}')"
echo "mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
echo "installed=$(cat /usr/lib/intentqos/VERSION 2>/dev/null)"
echo "lan_ip=$(uci -q get network.lan.ipaddr | cut -d/ -f1)"
echo "python=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
if ping -c 1 -W 3 1.1.1.1 >/dev/null 2>&1 || ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1; then echo internet=yes; else echo internet=no; fi
echo "agent=$(uci -q get intentqos.agent.enabled)"
echo "controller=$(uci -q get intentqos.agent.controller_url)"`
	out, _ := sshRun(c, script)
	f := map[string]string{}
	for _, ln := range strings.Split(out, "\n") {
		if k, v, ok := strings.Cut(strings.TrimSpace(ln), "="); ok {
			f[k] = v
		}
	}
	return f
}

// ------------------------------------------------------------------ install job

type job struct {
	mu       sync.Mutex
	Lines    []string       `json:"lines"`
	Done     bool           `json:"done"`
	OK       bool           `json:"ok"`
	Error    string         `json:"error,omitempty"`
	Result   map[string]any `json:"result,omitempty"`
	Paired   bool           `json:"paired"`
	PairErr  string         `json:"pair_error,omitempty"`
	Enrolled bool           `json:"enrolled"`
}

var cur *job
var curMu sync.Mutex

func (j *job) add(s string) {
	j.mu.Lock()
	j.Lines = append(j.Lines, s)
	j.mu.Unlock()
	logf("%s", s)
}

func (j *job) finish(ok bool, err error) {
	j.mu.Lock()
	j.Done, j.OK = true, ok
	if err != nil {
		j.Error = err.Error()
	}
	j.mu.Unlock()
}

type installReq struct {
	Down    float64 `json:"down"`
	Up      float64 `json:"up"`
	Pair    bool    `json:"pair"`
	PIN     string  `json:"pin"`
	Console string  `json:"console"` // https URL of the console (from discovery) or an address typed by the user
}

var resultRe = regexp.MustCompile(`^IQ-RESULT (\{.*\})\s*$`)

func runInstall(c *ssh.Client, req installReq, j *job, extra string) (map[string]any, error) {
	j.add("Uploading IntentQoS to the router (" + itoa(len(routerPayload)/1024) + " KiB)")
	if err := sshUpload(c, routerPayload, "/tmp/intentqos-router.tar.gz"); err != nil {
		return nil, err
	}
	if out, err := sshRun(c, "cd /tmp && rm -rf intentqos-router && tar xzf intentqos-router.tar.gz && rm -f intentqos-router.tar.gz"); err != nil {
		return nil, fmt.Errorf("could not unpack on the router: %v %s", err, out)
	}
	cmd := fmt.Sprintf("sh /tmp/intentqos-router/install.sh --down %g --up %g %s", req.Down, req.Up, extra)
	var result map[string]any
	var fail string
	err := sshStream(c, cmd, func(ln string) {
		switch {
		case strings.HasPrefix(ln, "IQ-STEP "):
			j.add(strings.TrimPrefix(ln, "IQ-STEP "))
		case strings.HasPrefix(ln, "IQ-WARN "):
			j.add("⚠ " + strings.TrimPrefix(ln, "IQ-WARN "))
		case strings.HasPrefix(ln, "IQ-FAIL "):
			fail = strings.TrimPrefix(ln, "IQ-FAIL ")
			j.add("✗ " + fail)
		default:
			if m := resultRe.FindStringSubmatch(ln); m != nil {
				_ = json.Unmarshal([]byte(m[1]), &result)
			}
		}
	})
	if fail != "" {
		return nil, errors.New(fail)
	}
	if err != nil {
		return nil, fmt.Errorf("installer stopped: %v", err)
	}
	if result == nil {
		return nil, errors.New("the installer finished without a result (see /tmp/intentqos-install.log on the router)")
	}
	return result, nil
}

func pairRouter(c *ssh.Client, req installReq, res map[string]any, j *job) error {
	ci, err := resolveConsole(req.Console)
	if err != nil {
		return err
	}
	lanDev, _ := res["lan"].(string)
	mac, _ := sshRun(c, "cat /sys/class/net/"+shq(lanDev)+"/address 2>/dev/null || cat /sys/class/net/eth0/address")
	serial := "IQ-" + strings.ToUpper(strings.ReplaceAll(strings.TrimSpace(mac), ":", ""))
	if len(serial) < 8 {
		serial = "IQ-" + strings.ReplaceAll(fmt.Sprint(res["lan_ip"]), ".", "")
	}
	model := strings.TrimSpace(rc.facts["model"])
	if model == "" {
		model = "OpenWrt router"
	}
	body := map[string]any{"pin": strings.ReplaceAll(req.PIN, " ", ""), "role": "router", "serial": serial, "name": model,
		"hw_model": model, "lan_ip": res["lan_ip"], "router_key": res["key"], "down_mbit": req.Down, "up_mbit": req.Up,
		"firmware": res["firmware"], "version": res["version"]}
	var b struct {
		ControllerURL string `json:"controller_url"`
		CA            string `json:"ca_pem"`
		Pub           string `json:"release_pubkey"`
		Claim         string `json:"claim_code"`
		Serial        string `json:"serial"`
	}
	j.add("Pairing with the Operator Console at " + ci.IP)
	if err := callJSON(consoleClient(""), "POST", strings.TrimRight(ci.HTTPS, "/")+"/v1/kit/pair", "", body, &b); err != nil {
		return fmt.Errorf("pairing failed: %v", err)
	}
	// outside the payload folder: runInstall unpacks the payload again, which replaces that folder
	if err := sshUpload(c, []byte(b.CA), "/tmp/intentqos-operator-ca.pem"); err != nil {
		return err
	}
	extra := fmt.Sprintf("--no-deps --controller %s --claim %s --pubkey %s --ca /tmp/intentqos-operator-ca.pem --serial %s",
		shq(b.ControllerURL), shq(b.Claim), shq(b.Pub), shq(b.Serial))
	if _, err := runInstall(c, req, j, extra); err != nil {
		return err
	}
	prof := loadProfile()
	prof.Console, prof.ConsoleCA = ci, b.CA
	prof.upsertRouter(RouterInfo{Serial: b.Serial, Name: model, LanIP: fmt.Sprint(res["lan_ip"]), Key: fmt.Sprint(res["key"]),
		Console: fmt.Sprint(res["console"]), Firmware: fmt.Sprint(res["firmware"]), Version: fmt.Sprint(res["version"])})
	_ = prof.save()
	j.add("Paired: the router now connects to the Operator Console by itself")
	return nil
}

func resolveConsole(s string) (*ConsoleInfo, error) {
	s = strings.TrimSpace(s)
	if s == "" || strings.HasPrefix(s, "https://") {
		for _, c := range discoverConsoles(1500 * time.Millisecond) {
			if s == "" || strings.TrimRight(c.HTTPS, "/") == strings.TrimRight(s, "/") {
				cc := c
				return &cc, nil
			}
		}
		if s == "" {
			return nil, errors.New("no Operator Console found on this network: start IntentQoS Operator Console, or type its address")
		}
	}
	return consoleAt(s)
}

func waitEnrolled(c *ssh.Client, j *job, limit time.Duration) bool {
	deadline := time.Now().Add(limit)
	for time.Now().Before(deadline) {
		out, _ := sshRun(c, "intentqos-ctl status")
		var st struct {
			Enrolled int `json:"enrolled"`
		}
		if json.Unmarshal([]byte(strings.TrimSpace(out)), &st) == nil && st.Enrolled == 1 {
			return true
		}
		time.Sleep(2 * time.Second)
	}
	return false
}

func installJob(req installReq) *job {
	j := &job{}
	curMu.Lock()
	cur = j
	curMu.Unlock()
	go func() {
		rc.mu.Lock()
		c := rc.client
		rc.mu.Unlock()
		if c == nil {
			j.finish(false, errors.New("not connected to the router"))
			return
		}
		res, err := runInstall(c, req, j, "")
		if err != nil {
			j.finish(false, err)
			return
		}
		j.mu.Lock()
		j.Result = res
		j.mu.Unlock()
		prof := loadProfile()
		prof.upsertRouter(RouterInfo{Serial: "router-" + fmt.Sprint(res["lan_ip"]), Name: rc.facts["model"], LanIP: fmt.Sprint(res["lan_ip"]),
			Key: fmt.Sprint(res["key"]), Console: fmt.Sprint(res["console"]), Firmware: fmt.Sprint(res["firmware"]), Version: fmt.Sprint(res["version"])})
		_ = prof.save()
		if req.Pair {
			if err := pairRouter(c, req, res, j); err != nil {
				j.add("✗ " + err.Error())
				j.mu.Lock()
				j.PairErr = err.Error()
				j.mu.Unlock()
			} else {
				j.mu.Lock()
				j.Paired = true
				j.mu.Unlock()
				j.add("Waiting for the router to enrol with the console")
				ok := waitEnrolled(c, j, 60*time.Second)
				j.mu.Lock()
				j.Enrolled = ok
				j.mu.Unlock()
				if ok {
					j.add("Router enrolled and reporting to the Operator Console")
				} else {
					j.add("⚠ The router has not enrolled yet. It keeps trying; check that the console laptop is on the same network")
				}
			}
		}
		_, _ = sshRun(c, "rm -rf /tmp/intentqos-router /tmp/intentqos-operator-ca.pem")
		j.finish(true, nil)
	}()
	return j
}

// ------------------------------------------------------------------ finding the router

type candidate struct {
	IP    string `json:"ip"`
	SSH   bool   `json:"ssh"`
	Web   bool   `json:"web"`
	Label string `json:"label"`
}

func findRouters() []candidate {
	ips := defaultGateways()
	labels := map[string]string{}
	for _, g := range ips {
		labels[g] = "this laptop's router (default gateway)"
	}
	for _, x := range []string{"192.168.8.1", "192.168.1.1"} {
		if _, ok := labels[x]; !ok {
			labels[x] = map[string]string{"192.168.8.1": "GL.iNet default address", "192.168.1.1": "OpenWrt default address"}[x]
			ips = append(ips, x)
		}
	}
	var mu sync.Mutex
	var wg sync.WaitGroup
	var out []candidate
	for _, ip := range uniq(ips) {
		wg.Add(1)
		go func(ip string) {
			defer wg.Done()
			c := candidate{IP: ip, Label: labels[ip], SSH: tcpOpen(ip, 22), Web: tcpOpen(ip, 80)}
			if c.SSH || c.Web {
				mu.Lock()
				out = append(out, c)
				mu.Unlock()
			}
		}(ip)
	}
	wg.Wait()
	order := map[string]int{}
	for i, ip := range ips {
		order[ip] = i
	}
	sort.Slice(out, func(a, b int) bool {
		if out[a].SSH != out[b].SSH {
			return out[a].SSH
		}
		return order[out[a].IP] < order[out[b].IP]
	})
	return out
}

func tcpOpen(ip string, port int) bool {
	c, err := net.DialTimeout("tcp", net.JoinHostPort(ip, itoa(port)), 1200*time.Millisecond)
	if err != nil {
		return false
	}
	c.Close()
	return true
}

// ------------------------------------------------------------------ program

func runPart(args []string) {
	fs := flag.NewFlagSet("router-setup", flag.ExitOnError)
	host := fs.String("host", "", "command-line mode: router address (e.g. 192.168.8.1)")
	user := fs.String("user", "root", "SSH user")
	pass := fs.String("password", "", "router password (or set INTENTQOS_ROUTER_PASSWORD)")
	down := fs.Float64("down", 100, "plan download speed, Mbit/s")
	up := fs.Float64("up", 100, "plan upload speed, Mbit/s")
	pin := fs.String("pin", "", "pair with the Operator Console using this PIN")
	console := fs.String("console", "", "Operator Console address (default: find it on the network)")
	uninstall := fs.Bool("uninstall", false, "remove IntentQoS from the router")
	purge := fs.Bool("purge", false, "with --uninstall: also delete settings and the router key")
	trust := fs.Bool("trust-new-hostkey", false, "accept a changed router SSH key (after a reset)")
	_ = fs.Parse(args)
	setupLog("router-setup")
	if *pass == "" {
		*pass = os.Getenv("INTENTQOS_ROUTER_PASSWORD")
	}
	if *host != "" {
		os.Exit(cliRouter(*host, *user, *pass, *down, *up, *pin, *console, *uninstall, *purge, *trust))
	}
	app := newApp()
	app.Mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write(webFile("router.html"))
	})
	app.Mux.HandleFunc("/api/find", func(w http.ResponseWriter, r *http.Request) {
		var cons []ConsoleInfo
		var routers []candidate
		var wg sync.WaitGroup
		wg.Add(2)
		go func() { defer wg.Done(); cons = discoverConsoles(1500 * time.Millisecond) }()
		go func() { defer wg.Done(); routers = findRouters() }()
		wg.Wait()
		writeJSON(w, 200, map[string]any{"routers": routers, "consoles": cons, "version": version})
	})
	app.Mux.HandleFunc("/api/connect", func(w http.ResponseWriter, r *http.Request) {
		var q struct {
			Host, User, Password string
			Trust                bool
		}
		if err := readJSON(r, &q); err != nil {
			errJSON(w, err)
			return
		}
		if q.User == "" {
			q.User = "root"
		}
		c, _, err := sshDial(q.Host, q.User, q.Password, q.Trust)
		if err != nil {
			errJSON(w, err)
			return
		}
		f := routerFacts(c)
		rc.mu.Lock()
		if rc.client != nil {
			rc.client.Close()
		}
		rc.client, rc.host, rc.facts = c, q.Host, f
		rc.mu.Unlock()
		if f["release"] == "" {
			errJSON(w, errors.New("this device is not running OpenWrt, prplOS or GL.iNet firmware"))
			return
		}
		writeJSON(w, 200, map[string]any{"facts": f, "payload_version": version})
	})
	app.Mux.HandleFunc("/api/install", func(w http.ResponseWriter, r *http.Request) {
		var q installReq
		if err := readJSON(r, &q); err != nil {
			errJSON(w, err)
			return
		}
		if q.Down <= 0 || q.Up <= 0 || q.Down > 10000 || q.Up > 10000 {
			errJSON(w, errors.New("enter your plan's download and upload speeds in Mbit/s"))
			return
		}
		installJob(q)
		writeJSON(w, 200, map[string]any{"ok": true})
	})
	app.Mux.HandleFunc("/api/pair", func(w http.ResponseWriter, r *http.Request) {
		var q installReq
		if err := readJSON(r, &q); err != nil {
			errJSON(w, err)
			return
		}
		curMu.Lock()
		prev := cur
		curMu.Unlock()
		if prev == nil || prev.Result == nil {
			errJSON(w, errors.New("install first"))
			return
		}
		j := &job{Result: prev.Result}
		curMu.Lock()
		cur = j
		curMu.Unlock()
		go func() {
			rc.mu.Lock()
			c := rc.client
			rc.mu.Unlock()
			if err := pairRouter(c, q, j.Result, j); err != nil {
				j.mu.Lock()
				j.PairErr = err.Error()
				j.mu.Unlock()
				j.add("✗ " + err.Error())
				j.finish(true, nil)
				return
			}
			j.mu.Lock()
			j.Paired = true
			j.mu.Unlock()
			ok := waitEnrolled(c, j, 60*time.Second)
			j.mu.Lock()
			j.Enrolled = ok
			j.mu.Unlock()
			_, _ = sshRun(c, "rm -rf /tmp/intentqos-router /tmp/intentqos-operator-ca.pem")
			j.finish(true, nil)
		}()
		writeJSON(w, 200, map[string]any{"ok": true})
	})
	app.Mux.HandleFunc("/api/uninstall", func(w http.ResponseWriter, r *http.Request) {
		var q struct{ Purge bool }
		_ = readJSON(r, &q)
		rc.mu.Lock()
		c := rc.client
		rc.mu.Unlock()
		if c == nil {
			errJSON(w, errors.New("not connected"))
			return
		}
		cmd := "sh /usr/lib/intentqos/uninstall.sh"
		if q.Purge {
			cmd += " --purge"
		}
		out, err := sshRun(c, cmd)
		if err != nil && !strings.Contains(out, `"removed":true`) {
			errJSON(w, fmt.Errorf("uninstall failed: %v %s", err, lastLine(out)))
			return
		}
		var steps []string
		for _, ln := range strings.Split(out, "\n") {
			if strings.HasPrefix(ln, "IQ-STEP ") {
				steps = append(steps, strings.TrimPrefix(ln, "IQ-STEP "))
			}
		}
		writeJSON(w, 200, map[string]any{"ok": true, "steps": steps})
	})
	app.Mux.HandleFunc("/api/job", func(w http.ResponseWriter, r *http.Request) {
		curMu.Lock()
		j := cur
		curMu.Unlock()
		if j == nil {
			writeJSON(w, 200, map[string]any{"idle": true})
			return
		}
		j.mu.Lock()
		defer j.mu.Unlock()
		writeJSON(w, 200, j)
	})
	url, err := app.Serve()
	if err != nil {
		msgBox(partTitle, err.Error())
		return
	}
	openWindow(url, "router-setup", 900, 820)
	app.WaitClosed(20 * time.Second)
}

func lastLine(s string) string {
	ls := strings.Split(strings.TrimSpace(s), "\n")
	return ls[len(ls)-1]
}

// cliRouter is the scripted form of the same steps (also used by the kit's end-to-end test).
func cliRouter(host, user, pass string, down, up float64, pin, console string, uninstall, purge, trust bool) int {
	c, _, err := sshDial(host, user, pass, trust)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return 2
	}
	defer c.Close()
	rc.client, rc.host, rc.facts = c, host, routerFacts(c)
	fmt.Printf("router: %s %s (%s), packages: %s, internet: %s, IntentQoS installed: %s\n", rc.facts["model"], rc.facts["description"],
		rc.facts["machine"], rc.facts["pm"], rc.facts["internet"], orNone(rc.facts["installed"]))
	if uninstall {
		cmd := "sh /usr/lib/intentqos/uninstall.sh"
		if purge {
			cmd += " --purge"
		}
		out, err := sshRun(c, cmd)
		fmt.Print(out)
		if err != nil {
			return 1
		}
		return 0
	}
	j := installJob(installReq{Down: down, Up: up, Pair: pin != "", PIN: pin, Console: console})
	printed := 0
	for {
		time.Sleep(300 * time.Millisecond)
		j.mu.Lock()
		for ; printed < len(j.Lines); printed++ {
			fmt.Println("  " + j.Lines[printed])
		}
		done, ok, e, res, perr := j.Done, j.OK, j.Error, j.Result, j.PairErr
		j.mu.Unlock()
		if done {
			if !ok {
				fmt.Fprintln(os.Stderr, "error:", e)
				return 1
			}
			b, _ := json.MarshalIndent(res, "", "  ")
			fmt.Println(string(b))
			if perr != "" {
				return 3
			}
			return 0
		}
	}
}

func orNone(s string) string {
	if s == "" {
		return "no"
	}
	return s
}
