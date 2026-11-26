// The kit settings file: one plain text file next to the programs that presets everything a program would
// otherwise ask for, so on filming day every program does its job from one double-click. Every key is
// optional; a missing file or key means the program asks, as before.
package main

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf16"
)

const settingsName = "IntentQoS Kit Settings.txt"

type Settings struct {
	TeamPIN        string
	RouterAddress  string
	RouterUser     string
	RouterPassword string
	PlanDown       float64
	PlanUp         float64
	Auto           bool
	Failover       bool
	Tests          []string
	TestSeconds    int
	Path           string
	Found          bool
	Warnings       []string
}

var pinRe = regexp.MustCompile(`^[0-9]{6}$`)

// settingsPath is the file next to this program (or $INTENTQOS_SETTINGS).
func settingsPath() string {
	if p := os.Getenv("INTENTQOS_SETTINGS"); p != "" {
		return p
	}
	exe, err := os.Executable()
	if err != nil {
		return settingsName
	}
	if r, err := filepath.EvalSymlinks(exe); err == nil {
		exe = r
	}
	return filepath.Join(filepath.Dir(exe), settingsName)
}

func loadSettings() Settings {
	p := settingsPath()
	b, err := os.ReadFile(p)
	if err != nil {
		return Settings{RouterUser: "root", Path: p}
	}
	s := parseSettings(b)
	s.Path, s.Found = p, true
	for _, w := range s.Warnings {
		logf("settings: %s", w)
	}
	return s
}

// decodeText turns Notepad's encodings (UTF-8 with or without BOM, UTF-16 LE/BE with BOM) into a string.
func decodeText(b []byte) string {
	switch {
	case bytes.HasPrefix(b, []byte{0xEF, 0xBB, 0xBF}):
		return string(b[3:])
	case len(b) >= 2 && (b[0] == 0xFF && b[1] == 0xFE || b[0] == 0xFE && b[1] == 0xFF):
		var order binary.ByteOrder = binary.LittleEndian
		if b[0] == 0xFE {
			order = binary.BigEndian
		}
		b = b[2:]
		u := make([]uint16, len(b)/2)
		for i := range u {
			u[i] = order.Uint16(b[2*i:])
		}
		return string(utf16.Decode(u))
	}
	return string(b)
}

func yes(v string) (bool, bool) {
	switch strings.ToLower(v) {
	case "yes", "y", "true", "on", "1":
		return true, true
	case "no", "n", "false", "off", "0":
		return false, true
	}
	return false, false
}

func parseSettings(b []byte) Settings {
	s := Settings{RouterUser: "root"}
	for n, ln := range strings.Split(strings.ReplaceAll(decodeText(b), "\r\n", "\n"), "\n") {
		if i := strings.Index(ln, "#"); i >= 0 {
			ln = ln[:i]
		}
		ln = strings.TrimSpace(ln)
		if ln == "" {
			continue
		}
		k, v, ok := strings.Cut(ln, "=")
		if !ok {
			s.Warnings = append(s.Warnings, fmt.Sprintf("line %d ignored (no '='): %q", n+1, ln))
			continue
		}
		k, v = strings.ToLower(strings.TrimSpace(k)), strings.TrimSpace(v)
		v = strings.Trim(v, `"'`)
		bad := func(what string) {
			s.Warnings = append(s.Warnings, fmt.Sprintf("%s: %s (line %d ignored)", k, what, n+1))
		}
		switch k {
		case "team_pin":
			p := strings.NewReplacer(" ", "", "-", "").Replace(v)
			if p == "" {
				continue
			}
			if !pinRe.MatchString(p) {
				bad("must be exactly 6 digits")
				continue
			}
			s.TeamPIN = p
		case "router_address":
			s.RouterAddress = v
		case "router_user":
			if v != "" {
				s.RouterUser = v
			}
		case "router_password":
			s.RouterPassword = v
		case "plan_down", "plan_up":
			if v == "" {
				continue
			}
			f, err := strconv.ParseFloat(v, 64)
			if err != nil || f <= 0 || f > 10000 {
				bad("must be a speed in Mbit/s, like 100")
				continue
			}
			if k == "plan_down" {
				s.PlanDown = f
			} else {
				s.PlanUp = f
			}
		case "auto", "failover":
			if v == "" {
				continue
			}
			t, ok := yes(v)
			if !ok {
				bad("must be yes or no")
				continue
			}
			if k == "auto" {
				s.Auto = t
			} else {
				s.Failover = t
			}
		case "tests":
			s.Tests = nil
			for _, t := range strings.Split(v, ",") {
				if t = strings.ToLower(strings.TrimSpace(t)); t != "" {
					s.Tests = append(s.Tests, t)
				}
			}
		case "test_seconds":
			if v == "" {
				continue
			}
			i, err := strconv.Atoi(v)
			if err != nil || i < 10 || i > 600 {
				bad("must be a number of seconds from 10 to 600")
				continue
			}
			s.TestSeconds = i
		default:
			s.Warnings = append(s.Warnings, fmt.Sprintf("unknown setting %q (line %d ignored)", k, n+1))
		}
	}
	return s
}

// settingsJSON is what a program's page sees: everything except the password.
func (s Settings) settingsJSON() map[string]any {
	return map[string]any{"found": s.Found, "path": s.Path, "team_pin": s.TeamPIN, "router_address": s.RouterAddress,
		"router_user": s.RouterUser, "has_password": s.RouterPassword != "", "plan_down": s.PlanDown, "plan_up": s.PlanUp,
		"auto": s.Auto, "failover": s.Failover, "tests": s.Tests, "test_seconds": s.TestSeconds, "warnings": s.Warnings}
}
