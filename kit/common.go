// Shared plumbing for the IntentQoS home-kit programs: data folders, logging, the app window (a local web
// page opened in an Edge/Chrome app window), discovery of the Operator Console, pairing, and the kit profile
// that programs on the same laptop share.
package main

import (
	"bytes"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
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

const discoveryPort = 47815

var version = "2.0.0"

// ------------------------------------------------------------------ folders and logging

// dataDir is where a program keeps its files: %LOCALAPPDATA%\IntentQoS\<sub> on Windows,
// ~/Library/Application Support/IntentQoS/<sub> on macOS, ~/.local/share/intentqos/<sub> on Linux.
func dataDir(sub string) string {
	var base string
	switch runtime.GOOS {
	case "windows":
		base = os.Getenv("LOCALAPPDATA")
		if base == "" {
			base, _ = os.UserConfigDir()
		}
		base = filepath.Join(base, "IntentQoS")
	case "darwin":
		h, _ := os.UserHomeDir()
		base = filepath.Join(h, "Library", "Application Support", "IntentQoS")
	default:
		if x := os.Getenv("XDG_DATA_HOME"); x != "" {
			base = filepath.Join(x, "intentqos")
		} else {
			h, _ := os.UserHomeDir()
			base = filepath.Join(h, ".local", "share", "intentqos")
		}
	}
	if d := os.Getenv("INTENTQOS_HOME"); d != "" { // tests and portable installs
		base = d
	}
	p := filepath.Join(base, sub)
	_ = os.MkdirAll(p, 0o700)
	return p
}

var logger = log.New(os.Stderr, "", log.LstdFlags)

func setupLog(name string) string {
	p := filepath.Join(dataDir("logs"), name+".log")
	if f, err := os.OpenFile(p, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600); err == nil {
		logger = log.New(io.MultiWriter(f, os.Stderr), "", log.LstdFlags)
	}
	return p
}

func logf(format string, a ...any) { logger.Printf(format, a...) }

// ------------------------------------------------------------------ the kit profile (kit.json)

// Profile is what programs on one laptop share: the paired console and the routers they know about.
type Profile struct {
	Console    *ConsoleInfo      `json:"console,omitempty"`
	ConsoleCA  string            `json:"console_ca,omitempty"`
	TestToken  string            `json:"testkit_token,omitempty"`
	Routers    []RouterInfo      `json:"routers,omitempty"`
	KnownHosts map[string]string `json:"known_hosts,omitempty"` // router address -> SSH host key fingerprint
	// router address -> its SSH password, base64 of protect() (Windows: DPAPI for this user), so a re-run signs in by itself
	RouterPasswords map[string]string `json:"router_passwords,omitempty"`
	Extra           map[string]any    `json:"extra,omitempty"`
}

type ConsoleInfo struct {
	Service string   `json:"service"`
	Name    string   `json:"name"`
	IP      string   `json:"ip"`
	HTTPS   string   `json:"https"`
	App     string   `json:"app"`
	Lab     *LabInfo `json:"lab,omitempty"`
}

// LabInfo: the console's in-home measurement endpoints (kit/lab.go); the fingerprint pins their certificate.
type LabInfo struct {
	Fingerprint string `json:"fingerprint"`
	HTTPSPort   int    `json:"https_port"`
	UDPPort     int    `json:"udp_port"`
}

type RouterInfo struct {
	Serial   string `json:"serial"`
	Name     string `json:"name"`
	LanIP    string `json:"lan_ip"`
	Key      string `json:"key,omitempty"`
	Console  string `json:"console,omitempty"`
	Firmware string `json:"firmware,omitempty"`
	Version  string `json:"version,omitempty"`
}

var profMu sync.Mutex

func profilePath() string { return filepath.Join(dataDir(""), "kit.json") }

func loadProfile() *Profile {
	profMu.Lock()
	defer profMu.Unlock()
	p := &Profile{}
	if b, err := os.ReadFile(profilePath()); err == nil {
		_ = json.Unmarshal(b, p)
	}
	if p.KnownHosts == nil {
		p.KnownHosts = map[string]string{}
	}
	if p.RouterPasswords == nil {
		p.RouterPasswords = map[string]string{}
	}
	return p
}

func (p *Profile) save() error {
	profMu.Lock()
	defer profMu.Unlock()
	b, _ := json.MarshalIndent(p, "", " ")
	tmp := profilePath() + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, profilePath())
}

func (p *Profile) upsertRouter(r RouterInfo) {
	for i := range p.Routers {
		if p.Routers[i].Serial == r.Serial || (r.LanIP != "" && p.Routers[i].LanIP == r.LanIP) {
			if r.Key == "" {
				r.Key = p.Routers[i].Key
			}
			p.Routers[i] = r
			return
		}
	}
	p.Routers = append(p.Routers, r)
}

// ------------------------------------------------------------------ discovery of the Operator Console

// discoverConsoles broadcasts IQKIT-DISCOVER on every IPv4 network of this machine and collects answers.
func discoverConsoles(wait time.Duration) []ConsoleInfo {
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4zero, Port: 0})
	if err != nil {
		logf("discovery: %v", err)
		return nil
	}
	defer conn.Close()
	targets := []string{"255.255.255.255", "127.0.0.1"}
	for _, n := range localNets() {
		targets = append(targets, broadcastAddr(n).String())
	}
	if extra := os.Getenv("INTENTQOS_CONSOLE"); extra != "" { // tests, or networks that block broadcast
		targets = append(targets, extra)
	}
	for _, t := range uniq(targets) {
		_, _ = conn.WriteToUDP([]byte("IQKIT-DISCOVER"), &net.UDPAddr{IP: net.ParseIP(t), Port: discoveryPort})
	}
	_ = conn.SetReadDeadline(time.Now().Add(wait))
	seen := map[string]bool{}
	var out []ConsoleInfo
	buf := make([]byte, 2048)
	for {
		n, _, err := conn.ReadFromUDP(buf)
		if err != nil {
			break
		}
		var c ConsoleInfo
		if json.Unmarshal(buf[:n], &c) == nil && c.Service == "intentqos-console" && !seen[c.HTTPS] {
			seen[c.HTTPS] = true
			out = append(out, c)
		}
	}
	// prefer a console on a real network address over loopback
	sort.SliceStable(out, func(i, j int) bool {
		return !strings.HasPrefix(out[i].IP, "127.") && strings.HasPrefix(out[j].IP, "127.")
	})
	return out
}

// consoleAt asks a console at a typed address (when broadcast is blocked) where it is.
func consoleAt(host string) (*ConsoleInfo, error) {
	host = strings.TrimSpace(strings.TrimPrefix(strings.TrimPrefix(host, "https://"), "http://"))
	host = strings.Split(host, "/")[0]
	h := host
	if !strings.Contains(h, ":") {
		h += ":8088"
	}
	c := &http.Client{Timeout: 4 * time.Second}
	r, err := c.Get("http://" + h + "/v1/kit/where")
	if err != nil {
		return nil, fmt.Errorf("no Operator Console at %s: %v", host, err)
	}
	defer r.Body.Close()
	var ci ConsoleInfo
	if err := json.NewDecoder(r.Body).Decode(&ci); err != nil || ci.HTTPS == "" {
		return nil, fmt.Errorf("%s is not an IntentQoS Operator Console in kit mode", host)
	}
	return &ci, nil
}

func localNets() []*net.IPNet {
	var out []*net.IPNet
	ifs, _ := net.Interfaces()
	for _, ifc := range ifs {
		if ifc.Flags&net.FlagUp == 0 || ifc.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, _ := ifc.Addrs()
		for _, a := range addrs {
			if n, ok := a.(*net.IPNet); ok && n.IP.To4() != nil {
				out = append(out, n)
			}
		}
	}
	return out
}

func broadcastAddr(n *net.IPNet) net.IP {
	ip := n.IP.To4()
	b := make(net.IP, 4)
	for i := range b {
		b[i] = ip[i] | ^n.Mask[i]
	}
	return b
}

func uniq(xs []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, x := range xs {
		if !seen[x] {
			seen[x] = true
			out = append(out, x)
		}
	}
	return out
}

// ------------------------------------------------------------------ HTTPS to the console

// consoleClient trusts the console's own CA once we have it (from pairing); before pairing it accepts the
// console's certificate (the 6-digit PIN authenticates the pairing) and pins the CA it returns.
func consoleClient(caPEM string) *http.Client {
	cfg := &tls.Config{MinVersion: tls.VersionTLS12}
	if caPEM != "" {
		pool := x509.NewCertPool()
		pool.AppendCertsFromPEM([]byte(caPEM))
		cfg.RootCAs = pool
	} else {
		cfg.InsecureSkipVerify = true //nolint:gosec // pairing: the PIN authenticates; the returned CA is pinned
	}
	return &http.Client{Timeout: 20 * time.Second, Transport: &http.Transport{TLSClientConfig: cfg, Proxy: nil}}
}

type apiError struct {
	Code int
	Msg  string
}

func (e *apiError) Error() string { return e.Msg }

func callJSON(c *http.Client, method, url, bearer string, body, out any) error {
	var rd io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, url, rd)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if bearer != "" {
		req.Header.Set("Authorization", "Bearer "+bearer)
	}
	r, err := c.Do(req)
	if err != nil {
		return err
	}
	defer r.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(r.Body, 8<<20))
	if r.StatusCode >= 300 {
		var e struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(raw, &e)
		if e.Error == "" {
			e.Error = fmt.Sprintf("HTTP %d", r.StatusCode)
		}
		return &apiError{r.StatusCode, e.Error}
	}
	if out != nil {
		return json.Unmarshal(raw, out)
	}
	return nil
}

// ------------------------------------------------------------------ the app window

// App is a local web UI: a page served on 127.0.0.1 and opened in an app window. The program ends when the
// window has been closed for a while (the page sends a heartbeat every few seconds).
type App struct {
	Mux     *http.ServeMux
	URL     string
	lastHB  atomic.Int64
	KeepRun func() bool // return true to stay alive without a window (e.g. a running console)
}

func newApp() *App {
	a := &App{Mux: http.NewServeMux()}
	a.Mux.HandleFunc("/hb", func(w http.ResponseWriter, r *http.Request) {
		a.lastHB.Store(time.Now().Unix())
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.WriteHeader(204)
	})
	a.Mux.HandleFunc("/kit.css", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/css; charset=utf-8")
		_, _ = w.Write(webFile("kit.css"))
	})
	a.Mux.HandleFunc("/icon.png", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "image/png")
		_, _ = w.Write(webFile("icon.png"))
	})
	return a
}

// Serve starts the local server and returns its URL.
func (a *App) Serve() (string, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", err
	}
	srv := &http.Server{Handler: localOnly(a.Mux), ReadHeaderTimeout: 10 * time.Second}
	go func() { _ = srv.Serve(ln) }()
	a.URL = fmt.Sprintf("http://%s/", ln.Addr().String())
	logf("page: %s", a.URL)
	return a.URL, nil
}

// localOnly refuses requests that did not come from this machine or that carry a foreign Origin (so a
// web page in another tab cannot drive the program).
func localOnly(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		host, _, _ := net.SplitHostPort(r.RemoteAddr)
		if host != "127.0.0.1" && host != "::1" {
			http.Error(w, "local only", 403)
			return
		}
		if o := r.Header.Get("Origin"); o != "" && r.URL.Path != "/hb" && !strings.HasPrefix(o, "http://127.0.0.1:") {
			http.Error(w, "foreign origin", 403)
			return
		}
		h.ServeHTTP(w, r)
	})
}

// WaitClosed blocks until the window has been gone for `grace` (after it was seen at least once).
func (a *App) WaitClosed(grace time.Duration) {
	start := time.Now()
	for {
		time.Sleep(2 * time.Second)
		last := a.lastHB.Load()
		if last == 0 {
			if time.Since(start) > 3*time.Minute { // the window never appeared
				if a.KeepRun != nil && a.KeepRun() {
					continue
				}
				return
			}
			continue
		}
		if time.Since(time.Unix(last, 0)) > grace {
			if a.KeepRun != nil && a.KeepRun() {
				continue
			}
			return
		}
	}
}

// openWindow shows url in an app-style browser window with its own profile; falls back to the default browser.
func openWindow(url, part string, w, h int) {
	if os.Getenv("INTENTQOS_NO_BROWSER") != "" {
		return
	}
	prof := filepath.Join(dataDir("browser"), part)
	args := []string{"--app=" + url, "--user-data-dir=" + prof, fmt.Sprintf("--window-size=%d,%d", w, h),
		"--no-first-run", "--no-default-browser-check", "--disable-features=Translate"}
	for _, b := range browsers() {
		if _, err := os.Stat(b); err == nil || (!strings.Contains(b, string(os.PathSeparator)) && lookPath(b)) {
			cmd := exec.Command(b, args...)
			hideWindow(cmd)
			if err := cmd.Start(); err == nil {
				logf("window: %s", b)
				go func() { _ = cmd.Wait() }()
				return
			}
		}
	}
	switch runtime.GOOS {
	case "windows":
		_ = exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Start()
	case "darwin":
		_ = exec.Command("open", url).Start()
	default:
		_ = exec.Command("xdg-open", url).Start()
	}
}

func lookPath(b string) bool { _, err := exec.LookPath(b); return err == nil }

func browsers() []string {
	switch runtime.GOOS {
	case "windows":
		var out []string
		for _, env := range []string{"ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"} {
			d := os.Getenv(env)
			if d == "" {
				continue
			}
			out = append(out, filepath.Join(d, `Microsoft\Edge\Application\msedge.exe`),
				filepath.Join(d, `Google\Chrome\Application\chrome.exe`), filepath.Join(d, `BraveSoftware\Brave-Browser\Application\brave.exe`))
		}
		return out
	case "darwin":
		return []string{"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
			"/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"}
	default:
		return []string{"microsoft-edge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser"}
	}
}

// ------------------------------------------------------------------ small helpers

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func readJSON(r *http.Request, v any) error {
	defer r.Body.Close()
	return json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(v)
}

func errJSON(w http.ResponseWriter, err error) {
	code := 400
	var ae *apiError
	if errors.As(err, &ae) && ae.Code >= 400 {
		code = ae.Code
	}
	writeJSON(w, code, map[string]string{"error": err.Error()})
}

func fingerprint(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

func machineName() string {
	h, err := os.Hostname()
	if err != nil || h == "" {
		return "laptop"
	}
	return h
}

// defaultGateways returns the IPv4 default gateway(s) of this machine, best first.
func defaultGateways() []string {
	var out []string
	switch runtime.GOOS {
	case "linux":
		if b, err := os.ReadFile("/proc/net/route"); err == nil {
			for _, ln := range strings.Split(string(b), "\n")[1:] {
				f := strings.Fields(ln)
				if len(f) > 3 && f[1] == "00000000" && f[2] != "00000000" {
					var x [4]byte
					if raw, err := hex.DecodeString(f[2]); err == nil && len(raw) == 4 {
						x = [4]byte{raw[3], raw[2], raw[1], raw[0]}
						out = append(out, net.IP(x[:]).String())
					}
				}
			}
		}
	case "windows":
		cmd := exec.Command("powershell", "-NoProfile", "-Command",
			"Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Sort-Object RouteMetric | Select-Object -ExpandProperty NextHop")
		hideWindow(cmd)
		if b, err := cmd.Output(); err == nil {
			for _, f := range strings.Fields(string(b)) {
				if ip := net.ParseIP(f); ip != nil && ip.To4() != nil && !ip.IsUnspecified() {
					out = append(out, f)
				}
			}
		}
	case "darwin":
		if b, err := exec.Command("route", "-n", "get", "default").Output(); err == nil {
			for _, ln := range strings.Split(string(b), "\n") {
				if strings.Contains(ln, "gateway:") {
					out = append(out, strings.TrimSpace(strings.SplitN(ln, ":", 2)[1]))
				}
			}
		}
	}
	return uniq(out)
}

// rememberPassword keeps a router's SSH password on this laptop (encrypted for this user on Windows).
func rememberPassword(host, pass string) {
	b, err := protect([]byte(pass))
	if err != nil {
		logf("could not protect the router password: %v", err)
		return
	}
	p := loadProfile()
	p.RouterPasswords[host] = base64.StdEncoding.EncodeToString(b)
	_ = p.save()
}

// savedPassword is the remembered password for a router ("", false when none).
func savedPassword(host string) (string, bool) {
	enc, ok := loadProfile().RouterPasswords[host]
	if !ok {
		return "", false
	}
	raw, err := base64.StdEncoding.DecodeString(enc)
	if err != nil {
		return "", false
	}
	b, err := unprotect(raw)
	if err != nil {
		return "", false
	}
	return string(b), true
}
