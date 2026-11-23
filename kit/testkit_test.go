//go:build testkit

package main

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"
)

func TestQoOMatchesTheEngine(t *testing.T) {
	// intentqos/qoo.py: voip p50 (60, 300), p99 (150, 500), loss (0.5, 5): p99 = 325 ms -> 50
	s := qooOf(qooReqs[1], 40, 325, 0)
	if s.QoO != 50 || s.Worst != "p99" {
		t.Fatalf("voice call QoO %v (%s), want 50 (p99)", s.QoO, s.Worst)
	}
	if g := qooOf(qooReqs[2], 20, 40, 0); g.QoO != 100 {
		t.Fatalf("an easy game should score 100, got %v", g.QoO)
	}
}

func TestTrimmedMeanDropsTheWorstFivePercent(t *testing.T) {
	x := make([]float64, 0, 100)
	for i := 0; i < 95; i++ {
		x = append(x, 10)
	}
	for i := 0; i < 5; i++ {
		x = append(x, 1000)
	}
	if m := trimmed(x); m != 10 {
		t.Fatalf("trimmed mean %v, want 10", m)
	}
}

func TestAssessAndReportRender(t *testing.T) {
	mk := func(base float64) arm {
		a := arm{QoS: "off"}
		for i := 0; i < 200; i++ {
			a.Samples = append(a.Samples, sample{T: float64(i) / 10, RTT: base + float64(i%20)})
		}
		stats(&a)
		return a
	}
	off, on := mk(120), mk(15)
	on.QoS = "on"
	if off.Quality == nil || on.Quality == nil || on.Quality.G != 15 || on.Quality.V99 < 18 {
		t.Fatalf("quality not filled: %+v", on.Quality)
	}
	if on.Quality.TRAI["latency_within"] != true || off.Quality.TRAI["latency_within"] != false {
		t.Fatalf("TRAI reference wrong: %+v / %+v", off.Quality.TRAI, on.Quality.TRAI)
	}
	on.RPM = &rpmResult{RPM: 900, Foreign: 1000, Loaded: 800, LoadedMs: 75, Note: "n"}
	off.RPM = &rpmResult{RPM: 300, Foreign: 500, Loaded: 100, LoadedMs: 600, Note: "n"}
	res := []scenarioResult{{ID: "game_downloads", Name: "Game while big downloads run", Off: off, On: on},
		{ID: "home_network", Name: "Home network only", Home: &homeResult{Capacity: &lanCapacity{MaxMbps: 400, SubMbps: []float64{300, 400}}}}}
	rep := buildReport("192.168.1.1", res, nil, time.Now())
	dir := t.TempDir()
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", dir)
	hp, jp, err := writeReport(rep)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(hp)
	h := string(b)
	for _, want := range []string{"Quality of Outcome", "Online game", "RPM", "400.0 Mbit/s", "TRAI"} {
		if !strings.Contains(h, want) {
			t.Fatalf("report lacks %q", want)
		}
	}
	if _, err := os.Stat(jp); err != nil {
		t.Fatal(err)
	}
	if s := summarize(res[0]); s["on_rpm"] != 900.0 || s["on_game_qoo"] == nil {
		t.Fatalf("summary: %v", s)
	}
}

func TestLabEndpointsOnLoopback(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	ls, err := startLab(ctx, 18446, 18447)
	if err != nil {
		t.Skip("cannot listen on loopback: ", err)
	}
	c, err := capacityTest(ctx, "127.0.0.1", 18447, 3*time.Second, nil)
	if err != nil {
		t.Fatal(err)
	}
	if c.MaxMbps < 5 || len(c.SubMbps) < 2 {
		t.Fatalf("capacity on loopback looks wrong: %+v", c)
	}
	cl := pinnedClient(ls.Fingerprint, 1)[0]
	urls, err := labConfigAt(cl, "127.0.0.1:18446")
	if err != nil {
		t.Fatal(err)
	}
	r := runRPM(ctx, urls["large_download_url"], urls["small_download_url"], urls["upload_url"], ls.Fingerprint, false, 11*time.Second, nil)
	if r == nil || r.RPM <= 0 || r.Probes < 10 || r.Conns < 2 {
		t.Fatalf("RPM on loopback: %+v", r)
	}
	if bad := pinnedClient(strings.Repeat("0", 64), 1)[0]; func() bool { _, e := labConfigAt(bad, "127.0.0.1:18446"); return e == nil }() {
		t.Fatal("a wrong fingerprint must be refused")
	}
}
