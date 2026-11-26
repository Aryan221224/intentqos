//go:build router

package main

import (
	"strings"
	"testing"
)

func TestRouterReady(t *testing.T) {
	f := map[string]string{"installed": "2.0.0", "agent": "1", "controller": "https://192.168.8.20:8443"}
	if !routerReady(f, "2.0.0", "https://192.168.8.20:8443/") {
		t.Fatal("same version and console must be ready")
	}
	f["installed"] = "1.9.0"
	if routerReady(f, "2.0.0", "https://192.168.8.20:8443") {
		t.Fatal("an older version must be updated")
	}
	f["installed"] = "2.0.0"
	f["controller"] = "https://10.0.0.5:8443"
	if routerReady(f, "2.0.0", "https://192.168.8.20:8443") {
		t.Fatal("a router paired with another console must be paired again")
	}
	if routerReady(map[string]string{"installed": "2.0.0", "agent": "1", "controller": ""}, "2.0.0", "") {
		t.Fatal("no console found: not ready")
	}
}

func TestPreflight(t *testing.T) {
	if e := preflight(map[string]string{"python": "", "internet": "no"}); !strings.Contains(e, "no internet") {
		t.Fatalf("no internet: %q", e)
	}
	if e := preflight(map[string]string{"python": "3.11", "internet": "no"}); e != "" {
		t.Fatalf("python present needs nothing: %q", e)
	}
	if e := preflight(map[string]string{"python": "", "internet": "yes", "free_kb": "9000"}); !strings.Contains(e, "free space") {
		t.Fatalf("flash: %q", e)
	}
	if e := preflight(map[string]string{"python": "", "internet": "yes", "free_kb": "60000"}); e != "" {
		t.Fatalf("fine router: %q", e)
	}
}

func TestRememberedPassword(t *testing.T) {
	t.Setenv("INTENTQOS_HOME", t.TempDir())
	if _, ok := savedPassword("192.168.8.1"); ok {
		t.Fatal("nothing saved yet")
	}
	rememberPassword("192.168.8.1", "goodpass")
	if pw, ok := savedPassword("192.168.8.1"); !ok || pw != "goodpass" {
		t.Fatalf("got %q %v", pw, ok)
	}
	b, _ := protect([]byte("x"))
	if out, _ := unprotect(b); string(out) != "x" {
		t.Fatal("protect round trip")
	}
}
