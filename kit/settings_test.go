package main

import (
	"encoding/binary"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf16"
)

func TestParseSettingsBasic(t *testing.T) {
	s := parseSettings([]byte("team_pin = 482 913\nplan_down=100 # Mbit/s\nplan_up = 40\nauto = yes\ntests = call, game\ntest_seconds = 30\nfailover = no\n"))
	if s.TeamPIN != "482913" || s.PlanDown != 100 || s.PlanUp != 40 || !s.Auto || s.Failover || s.TestSeconds != 30 {
		t.Fatalf("%+v", s)
	}
	if len(s.Tests) != 2 || s.Tests[0] != "call" || s.Tests[1] != "game" || s.RouterUser != "root" {
		t.Fatalf("%+v", s)
	}
	if len(s.Warnings) != 0 {
		t.Fatalf("unexpected warnings %v", s.Warnings)
	}
}

func TestParseSettingsBOMAndUTF16(t *testing.T) {
	if s := parseSettings([]byte("\xEF\xBB\xBFteam_pin=123456\r\n")); s.TeamPIN != "123456" {
		t.Fatalf("utf-8 BOM: %+v", s)
	}
	u := utf16.Encode([]rune("team_pin = 654321\r\nauto = on\r\n"))
	b := []byte{0xFF, 0xFE}
	for _, c := range u {
		b = binary.LittleEndian.AppendUint16(b, c)
	}
	if s := parseSettings(b); s.TeamPIN != "654321" || !s.Auto {
		t.Fatalf("utf-16le: %+v", s)
	}
	be := []byte{0xFE, 0xFF}
	for _, c := range u {
		be = binary.BigEndian.AppendUint16(be, c)
	}
	if s := parseSettings(be); s.TeamPIN != "654321" {
		t.Fatalf("utf-16be: %+v", s)
	}
}

func TestParseSettingsBadPIN(t *testing.T) {
	s := parseSettings([]byte("team_pin = 12345\nplan_down = fast\n"))
	if s.TeamPIN != "" || s.PlanDown != 0 {
		t.Fatalf("%+v", s)
	}
	if len(s.Warnings) != 2 || !strings.Contains(s.Warnings[0], "6 digits") {
		t.Fatalf("warnings %v", s.Warnings)
	}
}

func TestSettingsJSONHidesPassword(t *testing.T) {
	j := parseSettings([]byte("router_password = secret\n")).settingsJSON()
	if _, ok := j["router_password"]; ok || j["has_password"] != true {
		t.Fatalf("%v", j)
	}
	for _, v := range j {
		if s, ok := v.(string); ok && s == "secret" {
			t.Fatal("password leaked")
		}
	}
}

func TestLoadSettingsMissingFile(t *testing.T) {
	t.Setenv("INTENTQOS_SETTINGS", filepath.Join(t.TempDir(), "none.txt"))
	if s := loadSettings(); s.Found || s.RouterUser != "root" || s.Auto {
		t.Fatalf("%+v", s)
	}
}

func TestBusyPorts(t *testing.T) {
	ln, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Skip(err)
	}
	defer ln.Close()
	p := ln.Addr().(*net.TCPAddr).Port
	if got := busyPorts([]int{p}, nil); len(got) != 1 || got[0] != p {
		t.Fatalf("busyPorts = %v, want [%d]", got, p)
	}
	ln.Close()
	if got := busyPorts([]int{p}, nil); len(got) != 0 {
		t.Fatalf("free port reported busy: %v", got)
	}
}

func TestUnblockSiblingsNoPanic(t *testing.T) {
	d := t.TempDir()
	_ = os.WriteFile(filepath.Join(d, "IntentQoS Test Kit.exe"), []byte("x"), 0o644)
	unblockIn(d)
}
