//go:build bridge

// IntentQoS 4G Bridge: shares a phone's USB-tethered 4G connection over this laptop's Ethernet port, so the
// router's WAN cable can be moved here to test a fibre -> 4G failover. Windows uses Internet Connection
// Sharing; Linux uses NetworkManager's "shared" mode; macOS shows the two clicks in System Settings.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const partTitle = "IntentQoS 4G Bridge"

type adapter struct {
	Name   string   `json:"name"` // Windows connection name / Linux device
	Desc   string   `json:"desc"` // driver or description
	Up     bool     `json:"up"`
	IPs    []string `json:"ips"`
	Phone  bool     `json:"phone"`  // a USB-tethered phone
	Wired  bool     `json:"wired"`  // a physical Ethernet port
	Shared string   `json:"shared"` // "", "public" (shares its internet), "private" (receives the share)
}

// ------------------------------------------------------------------ Windows (Internet Connection Sharing)

const psList = `$ErrorActionPreference='SilentlyContinue'
$ips = @{}; Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { $ips[$_.InterfaceAlias] += @($_.IPAddress) }
$m = New-Object -ComObject HNetCfg.HNetShare
$sh = @{}
foreach ($c in $m.EnumEveryConnection) { $p = $m.NetConnectionProps.Invoke($c); $cfg = $m.INetSharingConfigurationForINetConnection.Invoke($c)
  if ($cfg.SharingEnabled) { $sh[$p.Name] = @('public','private')[$cfg.SharingConnectionType] } }
Get-NetAdapter | ForEach-Object { [pscustomobject]@{ name=$_.Name; desc=$_.InterfaceDescription; up=($_.Status -eq 'Up');
  ips=@($ips[$_.Name]); physical=$_.HardwareInterface; media=[string]$_.PhysicalMediaType; shared=[string]$sh[$_.Name] } } | ConvertTo-Json -Depth 3`

func winAdapters() ([]adapter, error) {
	cmd := exec.Command("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", psList)
	hideWindow(cmd)
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("could not list network adapters: %v", err)
	}
	var raw []struct {
		Name, Desc, Media, Shared string
		Up, Physical              bool
		IPs                       []string
	}
	s := strings.TrimSpace(string(out))
	if strings.HasPrefix(s, "{") {
		s = "[" + s + "]"
	}
	if err := json.Unmarshal([]byte(s), &raw); err != nil {
		return nil, err
	}
	var res []adapter
	for _, r := range raw {
		d := strings.ToLower(r.Desc)
		phone := strings.Contains(d, "remote ndis") || strings.Contains(d, "rndis") || strings.Contains(d, "apple mobile device ethernet") ||
			strings.Contains(d, "iphone") || strings.Contains(d, "android") || strings.Contains(d, "usb tethering")
		wired := !phone && r.Physical && (strings.Contains(strings.ToLower(r.Media), "802.3") || strings.Contains(d, "ethernet") || strings.Contains(d, "gbe")) &&
			!strings.Contains(d, "wi-fi") && !strings.Contains(d, "wireless") && !strings.Contains(d, "bluetooth")
		res = append(res, adapter{Name: r.Name, Desc: r.Desc, Up: r.Up, IPs: r.IPs, Phone: phone, Wired: wired, Shared: r.Shared})
	}
	return res, nil
}

func winShare(public, private string) error {
	script := fmt.Sprintf(`$ErrorActionPreference='Stop'
$m = New-Object -ComObject HNetCfg.HNetShare
$all = @(); foreach ($c in $m.EnumEveryConnection) { $all += [pscustomobject]@{ name=$m.NetConnectionProps.Invoke($c).Name; c=$c } }
foreach ($x in $all) { $cfg = $m.INetSharingConfigurationForINetConnection.Invoke($x.c); if ($cfg.SharingEnabled) { $cfg.DisableSharing() } }
$pub = ($all | Where-Object name -eq %s).c; $priv = ($all | Where-Object name -eq %s).c
if (-not $pub) { throw 'phone connection not found' }; if (-not $priv) { throw 'Ethernet connection not found' }
$m.INetSharingConfigurationForINetConnection.Invoke($pub).EnableSharing(0)
$m.INetSharingConfigurationForINetConnection.Invoke($priv).EnableSharing(1)`, psQuoteAny(public), psQuoteAny(private))
	return runPS(script)
}

func winUnshare() error {
	return runPS(`$m = New-Object -ComObject HNetCfg.HNetShare
foreach ($c in $m.EnumEveryConnection) { $cfg = $m.INetSharingConfigurationForINetConnection.Invoke($c); if ($cfg.SharingEnabled) { $cfg.DisableSharing() } }`)
}

func runPS(script string) error {
	cmd := exec.Command("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script)
	hideWindow(cmd)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%v: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func psQuoteAny(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }

// ------------------------------------------------------------------ Linux (NetworkManager)

const nmName = "IntentQoS 4G share"

func linuxAdapters() ([]adapter, error) {
	ifs, err := net.Interfaces()
	if err != nil {
		return nil, err
	}
	shared := ""
	if out, err := exec.Command("nmcli", "-t", "-f", "NAME,DEVICE", "con", "show", "--active").Output(); err == nil {
		for _, ln := range strings.Split(string(out), "\n") {
			if strings.HasPrefix(ln, nmName+":") {
				shared = strings.TrimPrefix(ln, nmName+":")
			}
		}
	}
	var res []adapter
	for _, i := range ifs {
		if i.Flags&net.FlagLoopback != 0 {
			continue
		}
		drv := ""
		if l, err := os.Readlink(filepath.Join("/sys/class/net", i.Name, "device", "driver")); err == nil {
			drv = filepath.Base(l)
		}
		_, wireless := os.Stat(filepath.Join("/sys/class/net", i.Name, "wireless"))
		phone := drv == "rndis_host" || drv == "cdc_ether" || drv == "ipheth" || drv == "cdc_ncm"
		wired := !phone && drv != "" && wireless != nil && !strings.HasPrefix(i.Name, "veth") && !strings.HasPrefix(i.Name, "docker")
		a := adapter{Name: i.Name, Desc: drv, Up: i.Flags&net.FlagUp != 0, Phone: phone, Wired: wired}
		addrs, _ := i.Addrs()
		for _, ad := range addrs {
			if n, ok := ad.(*net.IPNet); ok && n.IP.To4() != nil {
				a.IPs = append(a.IPs, n.IP.String())
			}
		}
		if i.Name == shared {
			a.Shared = "private"
		}
		res = append(res, a)
	}
	return res, nil
}

func linuxShare(eth string) error {
	_ = exec.Command("nmcli", "con", "delete", nmName).Run()
	if out, err := exec.Command("nmcli", "con", "add", "type", "ethernet", "ifname", eth, "con-name", nmName,
		"ipv4.method", "shared", "ipv6.method", "ignore", "connection.autoconnect", "no").CombinedOutput(); err != nil {
		return fmt.Errorf("nmcli: %v %s", err, out)
	}
	if out, err := exec.Command("nmcli", "con", "up", nmName).CombinedOutput(); err != nil {
		return fmt.Errorf("nmcli: %v %s", err, out)
	}
	return nil
}

func linuxUnshare() error {
	_ = exec.Command("nmcli", "con", "down", nmName).Run()
	return exec.Command("nmcli", "con", "delete", nmName).Run()
}

// ------------------------------------------------------------------ common

func adapters() ([]adapter, error) {
	switch runtime.GOOS {
	case "windows":
		return winAdapters()
	case "linux":
		return linuxAdapters()
	}
	return nil, errors.New("on a Mac: System Settings › General › Sharing › Internet Sharing: share the connection from your iPhone USB to your Ethernet port")
}

func share(phone, eth string) error {
	switch runtime.GOOS {
	case "windows":
		if isAdmin() {
			return winShare(phone, eth)
		}
		return runElevated("--share", phone, "--to", eth)
	case "linux":
		if _, err := exec.LookPath("nmcli"); err != nil {
			return errors.New("NetworkManager (nmcli) is needed on Linux")
		}
		return linuxShare(eth)
	}
	return errors.New("not supported here")
}

func unshare() error {
	switch runtime.GOOS {
	case "windows":
		if isAdmin() {
			return winUnshare()
		}
		return runElevated("--unshare")
	case "linux":
		return linuxUnshare()
	}
	return errors.New("not supported here")
}

func internetOK() bool {
	for _, t := range []string{"1.1.1.1:443", "8.8.8.8:443"} {
		if c, err := net.DialTimeout("tcp", t, 3*time.Second); err == nil {
			c.Close()
			return true
		}
	}
	return false
}

func runPart(args []string) {
	fs := flag.NewFlagSet("bridge", flag.ExitOnError)
	sh := fs.String("share", "", "share this connection (the phone) ...")
	to := fs.String("to", "", "... to this Ethernet connection")
	un := fs.Bool("unshare", false, "stop sharing")
	list := fs.Bool("list", false, "list network adapters and exit")
	_ = fs.Parse(args)
	setupLog("4g-bridge")
	switch {
	case *list:
		a, err := adapters()
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		b, _ := json.MarshalIndent(a, "", "  ")
		fmt.Println(string(b))
		return
	case *sh != "":
		if err := share(*sh, *to); err != nil {
			logf("share: %v", err)
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	case *un:
		if err := unshare(); err != nil {
			logf("unshare: %v", err)
			os.Exit(1)
		}
		return
	}
	app := newApp()
	app.Mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write(webFile("bridge.html"))
	})
	app.Mux.HandleFunc("/api/state", func(w http.ResponseWriter, r *http.Request) {
		a, err := adapters()
		res := map[string]any{"os": runtime.GOOS, "adapters": a, "internet": internetOK()}
		if err != nil {
			res["error"] = err.Error()
		}
		writeJSON(w, 200, res)
	})
	app.Mux.HandleFunc("/api/settings", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, loadSettings().settingsJSON())
	})
	app.Mux.HandleFunc("/api/share", func(w http.ResponseWriter, r *http.Request) {
		var q struct{ Phone, Eth string }
		if err := readJSON(r, &q); err != nil {
			errJSON(w, err)
			return
		}
		if err := share(q.Phone, q.Eth); err != nil {
			errJSON(w, err)
			return
		}
		writeJSON(w, 200, map[string]any{"ok": true})
	})
	app.Mux.HandleFunc("/api/unshare", func(w http.ResponseWriter, r *http.Request) {
		if err := unshare(); err != nil {
			errJSON(w, err)
			return
		}
		writeJSON(w, 200, map[string]any{"ok": true})
	})
	url, err := app.Serve()
	if err != nil {
		msgBox(partTitle, err.Error())
		return
	}
	openWindow(url, "4g-bridge", 860, 760)
	app.WaitClosed(20 * time.Second)
}
