//go:build windows

package main

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
	"syscall"

	"golang.org/x/sys/windows"
)

const createNoWindow = 0x08000000

// hideWindow keeps helper commands (powershell, netsh, python) from flashing console windows.
func hideWindow(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: createNoWindow}
}

func isAdmin() bool {
	return windows.GetCurrentProcessToken().IsElevated()
}

func psQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }

// runElevated starts this program again with administrator rights (the Windows UAC prompt) and waits for it.
func runElevated(args ...string) error {
	exe, _ := os.Executable()
	q := make([]string, len(args))
	for i, a := range args {
		q[i] = psQuote(syscall.EscapeArg(a))
	}
	script := fmt.Sprintf("$p = Start-Process -FilePath %s -ArgumentList @(%s) -Verb RunAs -Wait -PassThru -WindowStyle Hidden; exit $p.ExitCode",
		psQuote(exe), strings.Join(q, ","))
	cmd := exec.Command("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script)
	hideWindow(cmd)
	out, err := cmd.CombinedOutput()
	if err != nil {
		if strings.Contains(string(out), "canceled") || strings.Contains(string(out), "cancelled") {
			return fmt.Errorf("the administrator prompt was cancelled")
		}
		return fmt.Errorf("the administrator step failed: %v %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func msgBox(title, text string) {
	t, _ := windows.UTF16PtrFromString(title)
	m, _ := windows.UTF16PtrFromString(text)
	_, _ = windows.MessageBox(0, m, t, windows.MB_OK|windows.MB_ICONINFORMATION)
}
