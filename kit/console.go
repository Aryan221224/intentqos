//go:build console

// IntentQoS Operator Console: runs the fleet controller, the operator console and the phone-app server on
// this laptop, in home-kit mode. Everything it needs (a private Python runtime and the controller code) is
// inside the program; first start unpacks it into the data folder.
package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	_ "embed"
	"encoding/json"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"time"
)

//go:embed embed/app.zip
var appZip []byte

//go:embed embed/python.zip
var pyZip []byte // empty on macOS builds: the system python3 (3.11+) is used there

const partTitle = "IntentQoS Operator Console"

type consoleState struct {
	mu      sync.Mutex
	Stage   string   `json:"stage"`
	Steps   []string `json:"steps"`
	Error   string   `json:"error,omitempty"`
	Ready   bool     `json:"ready"`
	KitURL  string   `json:"kit_url,omitempty"`
	LogTail string   `json:"log_tail,omitempty"`
	Warn    []string `json:"warnings,omitempty"`
}

func (s *consoleState) step(msg string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Stage = msg
	s.Steps = append(s.Steps, msg)
	logf("%s", msg)
}

func (s *consoleState) warn(msg string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Warn = append(s.Warn, msg)
	logf("warning: %s", msg)
}

func (s *consoleState) fail(err error, tail string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Error, s.LogTail = err.Error(), tail
	logf("error: %v", err)
}

func runPart(args []string) {
	fs := flag.NewFlagSet("console", flag.ExitOnError)
	noWindow := fs.Bool("no-window", false, "run without opening a window (stop with Ctrl+C)")
	httpsPort := fs.Int("https-port", 8443, "port for routers and the pairing API (HTTPS)")
	appPort := fs.Int("app-port", 8088, "port for the phone app (plain HTTP on the home network)")
	fwOnly := fs.Bool("add-firewall-rules", false, "internal: add the Windows firewall rules (run elevated)")
	_ = fs.Parse(args)
	setupLog("console")
	logf("%s %s starting (%s/%s)", partTitle, version, runtime.GOOS, runtime.GOARCH)
	if *fwOnly {
		if err := addFirewallRules(*httpsPort, *appPort); err != nil {
			logf("firewall: %v", err)
			os.Exit(1)
		}
		os.Exit(0)
	}
	kitURL := fmt.Sprintf("http://127.0.0.1:%d/kit/", *appPort)
	// already running? just show it
	if consoleUp(*appPort) {
		logf("console already running; opening its window")
		if !*noWindow {
			openWindow(kitURL, "console", 1240, 860)
		}
		return
	}
	st := &consoleState{}
	app := newApp()
	app.Mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write(webFile("console_start.html"))
	})
	app.Mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		st.mu.Lock()
		defer st.mu.Unlock()
		writeJSON(w, 200, st)
	})
	url, err := app.Serve()
	if err != nil {
		msgBox(partTitle, "Could not start: "+err.Error())
		return
	}
	hbPort := strings.TrimSuffix(strings.TrimPrefix(url, "http://127.0.0.1:"), "/")
	st.KitURL = kitURL + "#hb=" + hbPort
	if !*noWindow {
		openWindow(url, "console", 1240, 860)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- runController(ctx, st, *httpsPort, *appPort) }()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	closed := make(chan struct{})
	if !*noWindow {
		go func() { app.WaitClosed(15 * time.Second); close(closed) }()
	}
	select {
	case <-sig:
	case <-closed:
		logf("window closed: stopping the console")
	case err := <-done:
		if err != nil {
			logf("controller ended: %v", err)
			if !*noWindow {
				app.WaitClosed(15 * time.Second) // keep the error page up until the window is closed
			}
		}
	}
	cancel()
	select {
	case <-done:
	case <-time.After(8 * time.Second):
	}
}

func consoleUp(appPort int) bool {
	c := &http.Client{Timeout: 1500 * time.Millisecond}
	r, err := c.Get(fmt.Sprintf("http://127.0.0.1:%d/v1/kit/where", appPort))
	if err != nil {
		return false
	}
	defer r.Body.Close()
	var ci ConsoleInfo
	return json.NewDecoder(r.Body).Decode(&ci) == nil && ci.Service == "intentqos-console"
}

// runController prepares everything and keeps the controller process running until ctx ends.
func runController(ctx context.Context, st *consoleState, httpsPort, appPort int) error {
	dir := dataDir("console")
	st.step("Preparing the console files")
	py, err := ensurePython(dir)
	if err != nil {
		st.fail(err, "")
		return err
	}
	appDir, err := ensureApp(dir)
	if err != nil {
		st.fail(err, "")
		return err
	}
	data := filepath.Join(dir, "data")
	_ = os.MkdirAll(filepath.Join(data, "pki"), 0o700)
	st.step("Creating this console's certificates")
	ips, err := ensurePKI(filepath.Join(data, "pki"))
	if err != nil {
		st.fail(err, "")
		return err
	}
	logf("certificate addresses: %s", strings.Join(ips, ", "))
	// The laptop may get its home address after the console started (it starts at sign-in, before Wi-Fi or the
	// cable is up). Routers check the address in the certificate, so re-issue it when a new address appears;
	// the controller reloads it. Found in the full-system emulation: a console started before its DHCP lease
	// had a certificate for 127.0.0.1 only and every router failed TLS ("IP address mismatch") until a restart.
	go watchAddresses(ctx, filepath.Join(data, "pki"), ips)
	cfgPath := filepath.Join(dir, "controller.toml")
	if err := writeConsoleConfig(cfgPath, data, httpsPort, appPort); err != nil {
		st.fail(err, "")
		return err
	}
	env := append(os.Environ(), "PYTHONPATH="+appDir, "PYTHONUTF8=1", "PYTHONDONTWRITEBYTECODE=1")
	// in-home measurement endpoints for the Test Kit (RPM over HTTPS/h2, RFC 9097-method UDP capacity)
	if lab, err := startLab(ctx, labHTTPSPort, labUDPPort); err == nil {
		env = append(env, "IQ_LAB_FINGERPRINT="+lab.Fingerprint, fmt.Sprintf("IQ_LAB_HTTPS_PORT=%d", labHTTPSPort),
			fmt.Sprintf("IQ_LAB_UDP_PORT=%d", labUDPPort))
		logf("lab endpoints on :%d (https) and :%d (udp), certificate %s", labHTTPSPort, labUDPPort, lab.Fingerprint[:16])
	} else {
		st.warn("In-home test endpoints not started: " + err.Error())
	}
	if _, err := os.Stat(filepath.Join(data, "controller.db")); errors.Is(err, os.ErrNotExist) {
		st.step("First start: creating the console database")
		cmd := exec.Command(py, "-m", "controller.seed", "--config", cfgPath, "--sim", "0", "--no-lab-gateway")
		cmd.Dir, cmd.Env = appDir, env
		hideWindow(cmd)
		out, err := cmd.CombinedOutput()
		_ = os.WriteFile(filepath.Join(data, "first-start.txt"), out, 0o600)
		if err != nil {
			e := fmt.Errorf("could not create the database: %v", err)
			st.fail(e, lastLines(string(out), 15))
			return e
		}
	}
	if runtime.GOOS == "windows" {
		if !firewallRulesPresent() {
			st.step("Allowing routers and phones to reach this laptop (Windows asks for permission once)")
			if err := runElevated("--add-firewall-rules", "--https-port", itoa(httpsPort), "--app-port", itoa(appPort)); err != nil {
				st.warn("Windows Firewall rules were not added (" + err.Error() + "). Routers and phones may not reach this laptop; " +
					"if Windows asks whether Python may use the network, allow it on private networks.")
			}
		}
	}
	logPath := filepath.Join(dataDir("logs"), "controller.log")
	restarts := 0
	for {
		st.step("Starting the controller")
		lf, _ := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		cmd := exec.Command(py, "-u", "-m", "controller.server", "--config", cfgPath)
		cmd.Dir, cmd.Env = appDir, env
		cmd.Stdout, cmd.Stderr = lf, lf
		hideWindow(cmd)
		if err := cmd.Start(); err != nil {
			lf.Close()
			st.fail(fmt.Errorf("could not start the controller: %v", err), "")
			return err
		}
		exited := make(chan error, 1)
		go func() { exited <- cmd.Wait() }()
		up := false
		deadline := time.Now().Add(60 * time.Second)
		for !up && time.Now().Before(deadline) {
			select {
			case err := <-exited:
				lf.Close()
				e := fmt.Errorf("the controller stopped while starting (%v)", err)
				st.fail(e, tailFile(logPath, 20))
				return e
			case <-ctx.Done():
				stopProc(cmd)
				lf.Close()
				return nil
			case <-time.After(500 * time.Millisecond):
				up = consoleUp(appPort)
			}
		}
		if !up {
			stopProc(cmd)
			lf.Close()
			e := errors.New("the controller did not answer within 60 s")
			st.fail(e, tailFile(logPath, 20))
			return e
		}
		st.mu.Lock()
		st.Ready, st.Error, st.Stage = true, "", "Running"
		st.mu.Unlock()
		logf("controller running: http://127.0.0.1:%d/kit/ (routers: HTTPS %d)", appPort, httpsPort)
		select {
		case <-ctx.Done():
			stopProc(cmd)
			lf.Close()
			return nil
		case err := <-exited:
			lf.Close()
			restarts++
			st.mu.Lock()
			st.Ready = false
			st.mu.Unlock()
			if restarts > 5 {
				e := fmt.Errorf("the controller keeps stopping (%v)", err)
				st.fail(e, tailFile(logPath, 20))
				return e
			}
			st.warn(fmt.Sprintf("the controller stopped (%v) and was restarted", err))
			time.Sleep(2 * time.Second)
		}
	}
}

func stopProc(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	if runtime.GOOS == "windows" {
		_ = cmd.Process.Kill()
	} else {
		_ = cmd.Process.Signal(syscall.SIGTERM)
		t := time.AfterFunc(5*time.Second, func() { _ = cmd.Process.Kill() })
		defer t.Stop()
	}
	_, _ = cmd.Process.Wait()
}

// ------------------------------------------------------------------ files

func ensurePython(dir string) (string, error) {
	if len(pyZip) < 100 { // macOS: system Python
		for _, p := range []string{"python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"} {
			if path, err := exec.LookPath(p); err == nil {
				out, _ := exec.Command(path, "-c", "import sys; print(sys.version_info >= (3, 11))").Output()
				if strings.TrimSpace(string(out)) == "True" {
					return path, nil
				}
			}
		}
		return "", errors.New("Python 3.11 or newer is needed: install it from python.org, then start the console again")
	}
	sum := fingerprint(pyZip)[:12]
	target := filepath.Join(dir, "runtime", sum)
	exe := filepath.Join(target, "python", "bin", "python3")
	if runtime.GOOS == "windows" {
		exe = filepath.Join(target, "python", "python.exe")
	}
	if _, err := os.Stat(exe); err == nil {
		return exe, nil
	}
	logf("unpacking the Python runtime to %s", target)
	tmp := target + ".tmp"
	_ = os.RemoveAll(tmp)
	if err := unzip(pyZip, tmp); err != nil {
		return "", fmt.Errorf("could not unpack the Python runtime: %v", err)
	}
	_ = os.RemoveAll(target)
	if err := os.Rename(tmp, target); err != nil {
		return "", err
	}
	cleanupOld(filepath.Join(dir, "runtime"), sum)
	return exe, nil
}

func ensureApp(dir string) (string, error) {
	sum := fingerprint(appZip)[:12]
	target := filepath.Join(dir, "app", sum)
	if _, err := os.Stat(filepath.Join(target, "controller", "server.py")); err == nil {
		return target, nil
	}
	tmp := target + ".tmp"
	_ = os.RemoveAll(tmp)
	if err := unzip(appZip, tmp); err != nil {
		return "", fmt.Errorf("could not unpack the console: %v", err)
	}
	_ = os.RemoveAll(target)
	if err := os.Rename(tmp, target); err != nil {
		return "", err
	}
	cleanupOld(filepath.Join(dir, "app"), sum)
	return target, nil
}

func cleanupOld(parent, keep string) {
	ents, _ := os.ReadDir(parent)
	for _, e := range ents {
		if e.Name() != keep && !strings.HasSuffix(e.Name(), ".tmp") {
			_ = os.RemoveAll(filepath.Join(parent, e.Name()))
		}
	}
}

func unzip(b []byte, dst string) error {
	zr, err := zip.NewReader(bytes.NewReader(b), int64(len(b)))
	if err != nil {
		return err
	}
	for _, f := range zr.File {
		p := filepath.Join(dst, filepath.FromSlash(f.Name))
		if !strings.HasPrefix(p, filepath.Clean(dst)+string(os.PathSeparator)) {
			return fmt.Errorf("bad path in archive: %s", f.Name)
		}
		if f.FileInfo().IsDir() {
			_ = os.MkdirAll(p, 0o755)
			continue
		}
		_ = os.MkdirAll(filepath.Dir(p), 0o755)
		rc, err := f.Open()
		if err != nil {
			return err
		}
		mode := f.Mode().Perm()
		if mode == 0 {
			mode = 0o644
		}
		w, err := os.OpenFile(p, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, mode)
		if err != nil {
			rc.Close()
			return err
		}
		_, err = io.Copy(w, rc)
		rc.Close()
		w.Close()
		if err != nil {
			return err
		}
	}
	return nil
}

func writeConsoleConfig(path, data string, httpsPort, appPort int) error {
	d := filepath.ToSlash(data)
	cfg := fmt.Sprintf(`# written by IntentQoS Operator Console on every start; settings that persist live in the database
listen = "0.0.0.0:%d"
data_dir = %q
tls_cert = %q
tls_key = %q
telemetry_interval_s = 15
demo_otp = true                 # home kit: the phone shows its own one-time code (there is no SMS gateway)
lab_quick_login = true          # operator console sign-in buttons, accepted only from this laptop
ui_listen = "0.0.0.0:%d"        # kit mode: phones on the home network get the app; everything else is this laptop only
auto_notify_outages = true
test_server = "https://speed.cloudflare.com/__down?bytes=1000000000"

[kit]
enabled = true

[brand]
name = "SampleNet Fibre"
short = "SampleNet"
support_phone = "1800-000-0000"
grievance_email = "grievance@samplenet.example"
accent = "#3987e5"

[retention]
raw_hours = 48
rollup_days = 90
events_days = 90
`, httpsPort, d, d+"/pki/server.pem", d+"/pki/server.key", appPort)
	return os.WriteFile(path, []byte(cfg), 0o600)
}

// ------------------------------------------------------------------ certificates

// ensurePKI keeps one CA for this console (routers trust it) and issues a fresh server certificate on every
// start for the addresses this laptop has right now, so a new DHCP address still passes TLS.
func ensurePKI(dir string) ([]string, error) {
	caKeyPath, caPath := filepath.Join(dir, "ca.key"), filepath.Join(dir, "ca.pem")
	var caKey *ecdsa.PrivateKey
	var caCert *x509.Certificate
	if kb, err := os.ReadFile(caKeyPath); err == nil {
		if cb, err2 := os.ReadFile(caPath); err2 == nil {
			kp, _ := pem.Decode(kb)
			cp, _ := pem.Decode(cb)
			if kp != nil && cp != nil {
				if k, err := x509.ParsePKCS8PrivateKey(kp.Bytes); err == nil {
					caKey, _ = k.(*ecdsa.PrivateKey)
				}
				caCert, _ = x509.ParseCertificate(cp.Bytes)
			}
		}
	}
	if caKey == nil || caCert == nil {
		k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if err != nil {
			return nil, err
		}
		tpl := &x509.Certificate{SerialNumber: serial(), Subject: pkix.Name{CommonName: "IntentQoS home kit CA (" + machineName() + ")"},
			NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().AddDate(10, 0, 0), IsCA: true, BasicConstraintsValid: true,
			KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageCRLSign, MaxPathLen: 0, MaxPathLenZero: true}
		der, err := x509.CreateCertificate(rand.Reader, tpl, tpl, &k.PublicKey, k)
		if err != nil {
			return nil, err
		}
		if err := writePEM(caKeyPath, "PRIVATE KEY", mustPKCS8(k), 0o600); err != nil {
			return nil, err
		}
		if err := writePEM(caPath, "CERTIFICATE", der, 0o644); err != nil {
			return nil, err
		}
		caKey = k
		caCert, _ = x509.ParseCertificate(der)
	}
	ips := []net.IP{net.ParseIP("127.0.0.1")}
	names := []string{"127.0.0.1"}
	for _, n := range localNets() {
		ips = append(ips, n.IP)
		names = append(names, n.IP.String())
	}
	k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	tpl := &x509.Certificate{SerialNumber: serial(), Subject: pkix.Name{CommonName: "IntentQoS Operator Console"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().AddDate(2, 0, 0), IPAddresses: ips,
		DNSNames: []string{"localhost", machineName()}, KeyUsage: x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	der, err := x509.CreateCertificate(rand.Reader, tpl, caCert, &k.PublicKey, caKey)
	if err != nil {
		return nil, err
	}
	if err := writePEM(filepath.Join(dir, "server.key"), "PRIVATE KEY", mustPKCS8(k), 0o600); err != nil {
		return nil, err
	}
	if err := writePEM(filepath.Join(dir, "server.pem"), "CERTIFICATE", der, 0o644); err != nil {
		return nil, err
	}
	return names, nil
}

func watchAddresses(ctx context.Context, dir string, have []string) {
	known := map[string]bool{}
	for _, a := range have {
		known[a] = true
	}
	t := time.NewTicker(10 * time.Second)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		var added []string
		for _, n := range localNets() {
			if a := n.IP.String(); !known[a] {
				added = append(added, a)
			}
		}
		if len(added) == 0 {
			continue
		}
		names, err := ensurePKI(dir)
		if err != nil {
			logf("could not re-issue the certificate for %s: %v", strings.Join(added, ", "), err)
			continue
		}
		known = map[string]bool{}
		for _, a := range names {
			known[a] = true
		}
		logf("new address %s: certificate re-issued for %s", strings.Join(added, ", "), strings.Join(names, ", "))
	}
}

func serial() *big.Int {
	n, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 120))
	return n
}

func mustPKCS8(k *ecdsa.PrivateKey) []byte {
	b, _ := x509.MarshalPKCS8PrivateKey(k)
	return b
}

func writePEM(path, typ string, der []byte, mode os.FileMode) error {
	return os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: typ, Bytes: der}), mode)
}

// ------------------------------------------------------------------ Windows firewall

const fwRule = "IntentQoS-Operator-Console" // no spaces: netsh parses name=... itself

func firewallRulesPresent() bool {
	for _, n := range []string{fwRule, fwRule + "-LabUDP"} { // older installs lack the lab rules: add them once
		cmd := exec.Command("netsh", "advfirewall", "firewall", "show", "rule", "name="+n)
		hideWindow(cmd)
		if cmd.Run() != nil {
			return false
		}
	}
	return true
}

func addFirewallRules(httpsPort, appPort int) error {
	del := exec.Command("netsh", "advfirewall", "firewall", "delete", "rule", "name="+fwRule)   // re-adding: no duplicates
	hideWindow(del)
	_ = del.Run()
	for _, n := range []string{"-Discovery", "-Lab", "-LabUDP"} {
		d := exec.Command("netsh", "advfirewall", "firewall", "delete", "rule", "name="+fwRule+n)
		hideWindow(d)
		_ = d.Run()
	}
	rules := [][]string{
		{"name=" + fwRule, "dir=in", "action=allow", "protocol=TCP", fmt.Sprintf("localport=%d,%d", httpsPort, appPort), "remoteip=localsubnet", "profile=any"},
		{"name=" + fwRule + "-Discovery", "dir=in", "action=allow", "protocol=UDP", fmt.Sprintf("localport=%d", discoveryPort), "remoteip=localsubnet", "profile=any"},
		{"name=" + fwRule + "-Lab", "dir=in", "action=allow", "protocol=TCP", fmt.Sprintf("localport=%d", labHTTPSPort), "remoteip=localsubnet", "profile=any"},
		{"name=" + fwRule + "-LabUDP", "dir=in", "action=allow", "protocol=UDP", fmt.Sprintf("localport=%d", labUDPPort), "remoteip=localsubnet", "profile=any"},
	}
	for _, r := range rules {
		cmd := exec.Command("netsh", append([]string{"advfirewall", "firewall", "add", "rule"}, r...)...)
		hideWindow(cmd)
		if out, err := cmd.CombinedOutput(); err != nil {
			return fmt.Errorf("netsh: %v %s", err, out)
		}
	}
	return nil
}

// ------------------------------------------------------------------ helpers

func tailFile(p string, n int) string {
	b, err := os.ReadFile(p)
	if err != nil {
		return ""
	}
	return lastLines(string(b), n)
}

func lastLines(s string, n int) string {
	ls := strings.Split(strings.TrimRight(s, "\n"), "\n")
	if len(ls) > n {
		ls = ls[len(ls)-n:]
	}
	return strings.Join(ls, "\n")
}
