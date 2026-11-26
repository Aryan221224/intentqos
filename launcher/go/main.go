//go:build windows

// IntentQoS launchers for Windows: one small program built five times (see launcher/build.py).
//
//	IntentQoS Gateway.exe            the lab home: emulated home network + the real engine, watchdog, agent
//	IntentQoS Operator Console.exe   the fleet controller with 240 simulated homes, operator console
//	SampleNet Home App.exe           the subscriber app (same controller)
//	IntentQoS Demo.exe               everything, three windows
//	Stop IntentQoS.exe               stop everything
//
// Everything runs inside WSL2 (the engine needs Linux network namespaces, tc and nftables). The
// launcher installs or updates the embedded release into /opt/intentqos on first use, starts the
// part it is for, keeps one hidden WSL session open so the services keep running after it exits,
// and opens the part in its own app window. Progress is shown in that window from a small local
// page the launcher serves on 127.0.0.1 until the service answers.
package main

import (
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode/utf16"
	"unsafe"
)

// set with -ldflags "-X main.part=gateway"
var part = "demo"

//go:embed app.zip
var appZip []byte

//go:embed wsl-setup.sh
var setupScript []byte

//go:embed starting.html
var startingHTML string

const appDir = "/opt/intentqos"

type window struct {
	Name, URL, Probe string
	W, H             int
}

type partDef struct {
	Title, Stack, Bg, Ink, Mute string
	Windows                     []window
}

var (
	gateway  = window{"gateway", "http://localhost:8090/", "http://127.0.0.1:8090/api/healthz", 1600, 1000}
	operator = window{"operator", "http://localhost:8088/ops/", "http://127.0.0.1:8088/healthz", 1600, 1000}
	app      = window{"app", "http://localhost:8088/app/", "http://127.0.0.1:8088/healthz", 430, 900}
	// the video demo: the gateway console plays the 80 s scripted demo by itself, with a caption per step
	gatewayAuto = window{"gateway", "http://localhost:8090/#autoplay", "http://127.0.0.1:8090/api/healthz", 1600, 1000}
)

var parts = map[string]partDef{
	"gateway":  {"Home gateway", "gateway", "#121922", "#E6ECF2", "#9AABBD", []window{gateway}},
	"operator": {"IntentQoS Fleet", "controller", "#F3F4F6", "#1B2130", "#576173", []window{operator}},
	"app":      {"SampleNet Fibre", "controller", "#1D2B5E", "#FFFFFF", "#C9D0EC", []window{app}},
	"demo":     {"IntentQoS", "all", "#121922", "#E6ECF2", "#9AABBD", []window{operator, app, gateway}},
	"video":    {"IntentQoS video demo", "all", "#121922", "#E6ECF2", "#9AABBD", []window{operator, app, gatewayAuto}},
}

var (
	base    string
	logFile *os.File
	distro  string
)

func logf(format string, a ...any) {
	line := time.Now().Format("2006-01-02 15:04:05 ") + fmt.Sprintf(format, a...) + "\n"
	if logFile != nil {
		logFile.WriteString(line)
	}
}

// ---------------------------------------------------------------------------------------- win32

var (
	user32      = syscall.NewLazyDLL("user32.dll")
	messageBoxW = user32.NewProc("MessageBoxW")
	sysParamsW  = user32.NewProc("SystemParametersInfoW")
)

// workArea is the desktop minus the taskbar, in the logical pixels a DPI-unaware program sees,
// which are the device-independent pixels the browser's --window-size and --window-position use.
func workArea() (x, y, w, h int) {
	var r struct{ L, T, R, B int32 }
	if ok, _, _ := sysParamsW.Call(0x0030 /* SPI_GETWORKAREA */, 0, uintptr(unsafe.Pointer(&r)), 0); ok == 0 || r.R <= r.L {
		return 0, 0, 1366, 728
	}
	return int(r.L), int(r.T), int(r.R - r.L), int(r.B - r.T)
}

// layout places each window inside the work area: one console fills it; the phone-sized app keeps its
// width and the full height; together, the consoles share the space left of the app, cascaded.
func layout(ws []window) [][4]int {
	ax, ay, aw, ah := workArea()
	out := make([][4]int, len(ws))
	appW := 0
	for _, w := range ws {
		if w.W < 600 {
			appW = min(w.W, aw/3)
		}
	}
	k := 0
	for i, w := range ws {
		if w.W < 600 {
			if len(ws) == 1 {
				out[i] = [4]int{ax + (aw-appW)/2, ay, appW, min(w.H, ah)}
			} else {
				out[i] = [4]int{ax + aw - appW, ay, appW, ah}
			}
			continue
		}
		cw, ch := aw-appW, ah
		off := 0
		if len(ws) > 2 {
			off = 28 * k
			cw, ch = cw-28, ch-28
		}
		out[i] = [4]int{ax + off, ay + off, min(w.W, cw), ch}
		k++
	}
	return out
}

// askYesNo shows a Yes/No question; true for Yes.
func askYesNo(title, text string) bool {
	t, _ := syscall.UTF16PtrFromString(text)
	c, _ := syscall.UTF16PtrFromString(title)
	r, _, _ := messageBoxW.Call(0, uintptr(unsafe.Pointer(t)), uintptr(unsafe.Pointer(c)), 0x4|0x20) // MB_YESNO | MB_ICONQUESTION
	return r == 6                                                                                    // IDYES
}

func msgBox(title, text string, icon uint32) {
	t, _ := syscall.UTF16PtrFromString(text)
	c, _ := syscall.UTF16PtrFromString(title)
	messageBoxW.Call(0, uintptr(unsafe.Pointer(t)), uintptr(unsafe.Pointer(c)), uintptr(icon))
}

const (
	mbIconError = 0x10
	mbIconInfo  = 0x40
	noWindow    = 0x08000000 // CREATE_NO_WINDOW
	newGroup    = 0x00000200 // CREATE_NEW_PROCESS_GROUP
)

func hidden(cmd *exec.Cmd, flags uint32) *exec.Cmd {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: noWindow | flags}
	return cmd
}

func alive(pid int) bool {
	h, err := syscall.OpenProcess(0x1000 /* PROCESS_QUERY_LIMITED_INFORMATION */, false, uint32(pid))
	if err != nil {
		return false
	}
	defer syscall.CloseHandle(h)
	var code uint32
	if syscall.GetExitCodeProcess(h, &code) != nil {
		return false
	}
	return code == 259 // STILL_ACTIVE
}

func kill(pid int) {
	h, err := syscall.OpenProcess(0x0001 /* PROCESS_TERMINATE */, false, uint32(pid))
	if err == nil {
		syscall.TerminateProcess(h, 0)
		syscall.CloseHandle(h)
	}
}

// ---------------------------------------------------------------------------------------- WSL

func utf16Out(b []byte) string {
	if len(b)%2 == 1 {
		b = b[:len(b)-1]
	}
	u := make([]uint16, len(b)/2)
	for i := range u {
		u[i] = uint16(b[2*i]) | uint16(b[2*i+1])<<8
	}
	return string(utf16.Decode(u))
}

// pickDistro returns a WSL 2 distribution, Ubuntu first.
func pickDistro() (string, error) {
	out, err := hidden(exec.Command("wsl.exe", "-l", "-v"), 0).Output()
	if err != nil {
		return "", errors.New("WSL is not installed")
	}
	type d struct {
		name string
		v2   bool
	}
	var list []d
	for _, ln := range strings.Split(strings.ReplaceAll(utf16Out(out), "\x00", ""), "\n") {
		f := strings.Fields(strings.TrimPrefix(strings.TrimSpace(ln), "*"))
		if len(f) < 3 || f[0] == "NAME" || strings.HasPrefix(strings.ToLower(f[0]), "docker") {
			continue
		}
		list = append(list, d{f[0], f[len(f)-1] == "2"})
	}
	for _, x := range list {
		if x.v2 && strings.HasPrefix(strings.ToLower(x.name), "ubuntu") {
			return x.name, nil
		}
	}
	for _, x := range list {
		if x.v2 {
			return x.name, nil
		}
	}
	if len(list) > 0 {
		return "", fmt.Errorf("the WSL distribution %q runs as WSL 1; IntentQoS needs WSL 2 (run: wsl --set-version %s 2)", list[0].name, list[0].name)
	}
	return "", errors.New("no Linux distribution is installed in WSL")
}

func wsl(args ...string) (string, error) {
	full := append([]string{"-d", distro, "-u", "root", "--"}, args...)
	out, err := hidden(exec.Command("wsl.exe", full...), 0).CombinedOutput()
	logf("wsl %s -> %v\n%s", strings.Join(args, " "), err, strings.TrimSpace(string(out)))
	return strings.TrimSpace(string(out)), err
}

func wslPath(win string) (string, error) {
	return wsl("wslpath", "-a", strings.ReplaceAll(win, `\`, `/`))
}

// ---------------------------------------------------------------------------------------- status page

type status struct {
	State  string   `json:"state"` // working | ready | error
	Msg    string   `json:"msg"`
	Steps  []string `json:"steps"`
	Step   int      `json:"step"`
	Target string   `json:"target"`
	Log    string   `json:"log"`
	Detail string   `json:"detail"`
}

// wslLogTail returns the service status, the end of its logs and recent kernel messages from WSL
// (`stack.sh diag`), shown on the error page.
func wslLogTail(stack string) string {
	out, _ := wsl("bash", appDir+"/scripts/stack.sh", "diag", stack)
	return out
}

var (
	mu     sync.Mutex
	st     = map[int]*status{}
	served = map[int]bool{}
)

func set(i int, f func(s *status)) {
	mu.Lock()
	defer mu.Unlock()
	f(st[i])
}

func serveStatus(p partDef) (string, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", err
	}
	page := startingHTML
	for k, v := range map[string]string{"{{TITLE}}": p.Title, "{{BG}}": p.Bg, "{{INK}}": p.Ink, "{{MUTE}}": p.Mute} {
		page = strings.ReplaceAll(page, k, v)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write([]byte(page))
	})
	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		i, _ := strconv.Atoi(r.URL.Query().Get("w"))
		mu.Lock()
		s := st[i]
		if s == nil {
			s = st[0]
		}
		b, _ := json.Marshal(s)
		if s.State == "ready" {
			served[i] = true
		}
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		w.Write(b)
	})
	go http.Serve(ln, mux)
	return "http://" + ln.Addr().String() + "/", nil
}

// ---------------------------------------------------------------------------------------- browser

func browser() string {
	for _, p := range []string{
		os.Getenv("ProgramFiles(x86)") + `\Microsoft\Edge\Application\msedge.exe`,
		os.Getenv("ProgramFiles") + `\Microsoft\Edge\Application\msedge.exe`,
		os.Getenv("ProgramFiles") + `\Google\Chrome\Application\chrome.exe`,
		os.Getenv("ProgramFiles(x86)") + `\Google\Chrome\Application\chrome.exe`,
		os.Getenv("LOCALAPPDATA") + `\Google\Chrome\Application\chrome.exe`,
	} {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

// openWindow opens url as an app window. Each window gets its own browser profile, so it starts its own
// browser process and the size and position switches apply to it (a second window in a running
// profile would ignore them).
func openWindow(url, profile string, r [4]int) {
	if b := browser(); b != "" {
		args := []string{"--app=" + url, fmt.Sprintf("--window-position=%d,%d", r[0], r[1]),
			fmt.Sprintf("--window-size=%d,%d", r[2], r[3]),
			"--user-data-dir=" + filepath.Join(base, "browser", profile), "--no-first-run", "--no-default-browser-check",
			"--disable-sync", "--disable-features=msImplicitSignin,msEdgeSidebarV2", "--hide-crash-restore-bubble", "--disable-extensions"}
		if err := exec.Command(b, args...).Start(); err == nil {
			return
		}
	}
	hidden(exec.Command("rundll32", "url.dll,FileProtocolHandler", url), 0).Start()
}

// ---------------------------------------------------------------------------------------- steps

func release() string {
	h := sha256.Sum256(appZip)
	return hex.EncodeToString(h[:6])
}

func ensureInstalled() (bool, error) {
	rel := release()
	cur, _ := wsl("cat", appDir+"/.release")
	if strings.TrimSpace(cur) == rel {
		return false, nil
	}
	zipWin := filepath.Join(base, "intentqos-"+rel+".zip")
	if _, err := os.Stat(zipWin); err != nil {
		if err := os.WriteFile(zipWin, appZip, 0o644); err != nil {
			return true, err
		}
	}
	setupWin := filepath.Join(base, "wsl-setup.sh")
	if err := os.WriteFile(setupWin, []byte(strings.ReplaceAll(string(setupScript), "\r\n", "\n")), 0o644); err != nil {
		return true, err
	}
	zp, err := wslPath(zipWin)
	if err != nil {
		return true, fmt.Errorf("cannot reach the release file from WSL: %v", err)
	}
	sp, err := wslPath(setupWin)
	if err != nil {
		return true, err
	}
	if out, err := wsl("bash", sp, zp, rel); err != nil {
		return true, fmt.Errorf("setup inside WSL failed: %s", lastLines(out, 4))
	}
	return true, nil
}

func lastLines(s string, n int) string {
	l := strings.Split(strings.TrimSpace(s), "\n")
	if len(l) > n {
		l = l[len(l)-n:]
	}
	return strings.Join(l, " / ")
}

// keeper holds one WSL session open, so WSL does not stop the distribution (and our services)
// after the launcher exits. Stop IntentQoS.exe ends it.
func keeper() error {
	pidFile := filepath.Join(base, "keeper.pid")
	if b, err := os.ReadFile(pidFile); err == nil {
		if pid, _ := strconv.Atoi(strings.TrimSpace(string(b))); pid > 0 && alive(pid) {
			return nil
		}
	}
	cmd := hidden(exec.Command("wsl.exe", "-d", distro, "-u", "root", "--", "bash", appDir+"/scripts/stack.sh", "hold"), newGroup)
	if err := cmd.Start(); err != nil {
		return err
	}
	logf("keeper pid %d", cmd.Process.Pid)
	return os.WriteFile(pidFile, []byte(strconv.Itoa(cmd.Process.Pid)), 0o644)
}

// waitHTTP polls url until it answers. Every 15 s it also asks WSL whether the service process is
// still alive (proc is its name in `stack.sh status`), so a crash is reported at once, not after the limit.
func waitHTTP(url, proc string, limit time.Duration) bool {
	c := http.Client{Timeout: 3 * time.Second, Transport: &http.Transport{Proxy: nil}}
	start := time.Now()
	last := start
	for time.Since(start) < limit {
		if r, err := c.Get(url); err == nil {
			r.Body.Close()
			if r.StatusCode < 500 {
				return true
			}
		}
		if proc != "" && time.Since(last) > 15*time.Second {
			last = time.Now()
			if out, _ := wsl("bash", appDir+"/scripts/stack.sh", "status"); strings.Contains(out, proc+" stopped") {
				time.Sleep(2 * time.Second)
				if out, _ := wsl("bash", appDir+"/scripts/stack.sh", "status"); strings.Contains(out, proc+" stopped") {
					return false
				}
			}
		}
		time.Sleep(time.Second)
	}
	return false
}

// closeWindows ends the browser processes that use the launchers' own profiles, which closes the
// IntentQoS windows and nothing else.
func closeWindows() {
	prof := strings.ReplaceAll(filepath.Join(base, "browser"), "'", "''")
	ps := "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" | " +
		"Where-Object { $_.CommandLine -like '*" + prof + "*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
	out, err := hidden(exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps), 0).CombinedOutput()
	logf("close windows -> %v %s", err, strings.TrimSpace(string(out)))
}

func stopAll() {
	closeWindows()
	if d, err := pickDistro(); err == nil {
		distro = d
		wsl("bash", appDir+"/scripts/stack.sh", "down", "all")
	}
	pidFile := filepath.Join(base, "keeper.pid")
	if b, err := os.ReadFile(pidFile); err == nil {
		if pid, _ := strconv.Atoi(strings.TrimSpace(string(b))); pid > 0 {
			kill(pid)
		}
		os.Remove(pidFile)
	}
}

func main() {
	base = filepath.Join(os.Getenv("LOCALAPPDATA"), "IntentQoS")
	os.MkdirAll(base, 0o755)
	logPath := filepath.Join(base, "launcher.log")
	logFile, _ = os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	logf("---- %s (release %s)", part, release())

	if part == "stop" {
		stopAll()
		msgBox("IntentQoS", "IntentQoS has stopped: the gateway lab, the controller and the simulated homes, and its windows are closed.", mbIconInfo)
		return
	}
	p, ok := parts[part]
	if !ok {
		msgBox("IntentQoS", "Unknown launcher part "+part, mbIconError)
		return
	}
	var err error
	distro, err = pickDistro()
	if err != nil {
		logf("no distro: %v", err)
		if askYesNo("IntentQoS needs WSL 2", "IntentQoS runs its gateway and controller inside Windows Subsystem for Linux (WSL 2), "+
			"because the engine needs Linux network namespaces, tc and nftables.\n\n("+err.Error()+")\n\n"+
			"Install WSL with Ubuntu now? Windows asks for administrator permission and downloads Ubuntu (about 500 MB, "+
			"a few minutes). Then restart Windows: an Ubuntu window opens once and asks for a user name and password "+
			"(any you like). After that, double-click this program again.") {
			cmd := exec.Command("powershell", "-NoProfile", "-Command",
				"Start-Process -FilePath wsl.exe -ArgumentList '--install','-d','Ubuntu' -Verb RunAs -Wait")
			hidden(cmd, 0)
			if out, err := cmd.CombinedOutput(); err != nil {
				logf("wsl --install: %v %s", err, out)
				msgBox("IntentQoS", "WSL was not installed ("+err.Error()+"). Open PowerShell as administrator and run:\n    wsl --install -d Ubuntu", mbIconError)
				return
			}
			msgBox("IntentQoS", "WSL is being installed. Restart Windows when it finishes, set up the Ubuntu user when its window "+
				"opens, then double-click this program again.", mbIconInfo)
		}
		return
	}
	steps := []string{"Find WSL (" + distro + ")", "Install or update IntentQoS in WSL", "Start the services", "Wait until they answer"}
	for i, w := range p.Windows {
		st[i] = &status{State: "working", Msg: "Checking WSL…", Steps: steps, Target: w.URL, Log: logPath}
	}
	url, err := serveStatus(p)
	if err != nil {
		msgBox("IntentQoS", "Could not start the launcher: "+err.Error(), mbIconError)
		return
	}
	rects := layout(p.Windows)
	for i, w := range p.Windows {
		openWindow(fmt.Sprintf("%s?w=%d", url, i), w.Name, rects[i])
		if i < len(p.Windows)-1 {
			time.Sleep(700 * time.Millisecond)
		}
	}
	all := func(f func(s *status)) {
		for i := range p.Windows {
			set(i, f)
		}
	}
	fail := func(msg string) {
		logf("error: %s", msg)
		all(func(s *status) { s.State, s.Msg = "error", msg })
		time.Sleep(60 * time.Second) // keep serving the error page for a while
	}

	all(func(s *status) { s.Step, s.Msg = 1, "Checking the installed release in WSL…" })
	cur, _ := wsl("cat", appDir+"/.release")
	if strings.TrimSpace(cur) != release() {
		all(func(s *status) {
			s.Msg = "Installing IntentQoS into WSL. The first time this also installs a few Linux tools and can take a few minutes."
		})
	}
	if _, err := ensureInstalled(); err != nil {
		fail(err.Error())
		return
	}
	if err := keeper(); err != nil {
		fail("could not keep a WSL session open: " + err.Error())
		return
	}
	all(func(s *status) { s.Step, s.Msg = 2, "Starting the services inside WSL…" })
	if out, err := wsl("bash", appDir+"/scripts/stack.sh", "up", p.Stack); err != nil {
		all(func(s *status) { s.Detail = out })
		fail("could not start the services: " + lastLines(out, 3))
		return
	}
	all(func(s *status) {
		s.Step = 3
		if p.Stack == "gateway" || p.Stack == "all" {
			s.Msg = "Building the emulated home network and starting the engine. This takes about a minute."
		} else {
			s.Msg = "Waiting for the controller and the simulated homes."
		}
	})
	var wg sync.WaitGroup
	for i, w := range p.Windows {
		wg.Add(1)
		go func(i int, w window) {
			defer wg.Done()
			proc := "controller"
			if w.Name == "gateway" {
				proc = "lab"
			}
			if waitHTTP(w.Probe, proc, 240*time.Second) {
				set(i, func(s *status) { s.State, s.Step, s.Msg = "ready", 4, "Ready." })
			} else {
				stack := "controller"
				if w.Name == "gateway" {
					stack = "gateway"
				}
				tail := wslLogTail(stack)
				set(i, func(s *status) {
					s.State = "error"
					s.Msg = "It stopped or did not answer within 4 minutes. The end of its log is below; all logs are in " + appDir + "/results-lab/logs inside WSL."
					s.Detail = tail
				})
			}
		}(i, w)
	}
	wg.Wait()
	// let every window pick up its final state, then exit; the services keep running in WSL
	for t := 0; t < 30; t++ {
		mu.Lock()
		n := len(served)
		mu.Unlock()
		if n >= len(p.Windows) {
			break
		}
		time.Sleep(time.Second)
	}
	time.Sleep(3 * time.Second)
	logf("done")
}
