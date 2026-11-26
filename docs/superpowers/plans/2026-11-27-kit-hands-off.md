# Home kit hands-off mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each kit program runs its job by itself from one double-click on a Windows laptop, with no typing and no prompts during a filmed take, and is safe to re-run for retakes.

**Architecture:** One shared settings file next to the programs, read by a new `kit/settings.go`; each program's existing page gains an automatic path driven by that file (the interactive path is unchanged). The controller gets a fixed team PIN and a one-scan phone sign-in link. Windows one-time prompts are moved into the first (rehearsal) run.

**Tech Stack:** Go 1.24 (kit programs, `golang.org/x/sys/windows`), Python 3.11+ stdlib (controller, engine), plain HTML/JS pages embedded in the programs.

**Spec:** `docs/superpowers/specs/2026-11-27-kit-hands-off-design.md`

## Global Constraints

- Interactive mode keeps working exactly as today when the settings file is absent or a key is missing.
- Settings file name: `IntentQoS Kit Settings.txt`, next to the program (`filepath.Dir(os.Executable())`); env `INTENTQOS_SETTINGS` overrides the path (tests).
- Keys: `team_pin`, `router_address`, `router_user` (default `root`), `router_password`, `plan_down`, `plan_up`, `auto`, `tests`, `test_seconds`, `failover`.
- Countdown before any automatic action: 5 s, with a Stop button.
- The 5-wrong-PINs / 5-minute lockout stays.
- No secrets in the repository; the shipped settings file has `router_password` empty.
- No commits or pushes (the user has not asked for them); every "commit" point below is a checkpoint only.
- Windows 10/11 is the target; Linux builds must still compile and pass tests.

## Review Focus

1. Settings saved by Notepad as UTF-8 with BOM or as UTF-16 → parsed the same as plain UTF-8 (test in Task 1).
2. `team_pin` typed with a space (`482 913`) or the wrong length → spaces ignored; anything not 6 digits is ignored with a warning and the program asks as today (test in Task 1, Python test in Task 2).
3. Router has an older IntentQoS version, or is paired with a different console → not "Ready": it is reinstalled/re-paired (test in Task 4).
4. Wrong preset or remembered password → the window asks for the password once; no retry loop that could lock the router (test in Task 4).
5. Phone opens an expired link (QR photographed earlier) → "scan the QR code on the console again", not a blank page (test in Task 3).

---

### Task 1: Shared settings file

**Files:**
- Create: `kit/settings.go`
- Test: `kit/settings_test.go`

**Interfaces:**
- Produces:
  - `type Settings struct { TeamPIN, RouterAddress, RouterUser, RouterPassword string; PlanDown, PlanUp float64; Auto, Failover bool; Tests []string; TestSeconds int; Path string; Warnings []string }`
  - `func parseSettings(b []byte) Settings` (handles UTF-8 BOM and UTF-16 LE/BE BOM; `#` starts a comment; keys case-insensitive; `yes/no/true/false/on/off/1/0`; `tests` comma-separated)
  - `func loadSettings() Settings` (reads the file at the path above; missing file → zero Settings with `RouterUser="root"`)
  - `func (s Settings) settingsJSON() map[string]any` (everything except the password, plus `has_password bool`, `found bool`)

- [ ] **Step 1: Write the failing tests** (`go test` without tags compiles `part_none.go`, so these run untagged)

```go
func TestParseSettingsBasic(t *testing.T) {
	s := parseSettings([]byte("team_pin = 482 913\nplan_down=100 # Mbit/s\nplan_up = 40\nauto = yes\ntests = call, game\ntest_seconds = 30\nfailover = no\n"))
	if s.TeamPIN != "482913" || s.PlanDown != 100 || s.PlanUp != 40 || !s.Auto || s.Failover || s.TestSeconds != 30 {
		t.Fatalf("%+v", s)
	}
	if len(s.Tests) != 2 || s.Tests[0] != "call" || s.Tests[1] != "game" || s.RouterUser != "root" { t.Fatalf("%+v", s) }
}
func TestParseSettingsBOMAndUTF16(t *testing.T)   // "\xEF\xBB\xBFteam_pin=123456" and UTF-16LE of the same → TeamPIN "123456"
func TestParseSettingsBadPIN(t *testing.T)         // team_pin = 12345 → TeamPIN "" and one warning mentioning "6 digits"
func TestSettingsJSONHidesPassword(t *testing.T)   // router_password = x → settingsJSON() has no "router_password", has_password true
```

- [ ] **Step 2: Run to verify they fail:** `cd kit && go test -run Settings ./...` → FAIL (undefined: parseSettings)
- [ ] **Step 3: Implement `kit/settings.go`** (decode UTF-16 with `unicode/utf16`; unknown keys → warning, not error)
- [ ] **Step 4: Run to verify they pass:** `cd kit && go test -run Settings ./...` → PASS; `go vet -tags console ./...` etc. still clean
- [ ] **Step 5: Checkpoint** (no commit)

### Task 2: Console uses the team PIN, firewall without prompts, busy-port check

**Files:**
- Modify: `kit/console.go` (`runPart`, `runController`, `writeConsoleConfig`, `addFirewallRules`, `firewallRulesPresent`)
- Modify: `controller/kit.py` (`Kit.__init__`)
- Create: `kit/ports.go` (untagged) — Test: `kit/settings_test.go` (same untagged test file)
- Test: `tests/test_kit_pin.py`

**Interfaces:**
- Consumes: `loadSettings()` (Task 1)
- Produces:
  - controller config `[kit] pin = "NNNNNN"` written only when `Settings.TeamPIN` is set; `Kit.pin` uses it when it is exactly 6 digits, else random (as today)
  - `func busyPorts(ports []int) []int` (tries `net.Listen("tcp", ":p")`; UDP for 47815 and 8447)
  - `addFirewallRules(httpsPort, appPort int, programs []string) error` adds, besides today's port rules, one allow rule per program path (`program=<path> dir=in action=allow profile=private,domain`) named `IntentQoS-Operator-Console-App<N>`, after `netsh advfirewall firewall delete rule name=all program="<path>" dir=in` for each path (removes Windows' auto-created block rules); `--add-firewall-rules` gains `--program` (repeatable)
  - `firewallRulesPresent(python string) bool` also checks that a rule for `python` exists (`show rule name=all verbose` contains the path)
  - console flag `--no-firewall`: skip the firewall step (local-only checks, Task 8)

- [ ] **Step 1: Failing tests**

```python
def test_team_pin_from_config(tmp_path):  # Kit with cfg["kit"]["pin"] = "482913" → kit.pin == "482913"
def test_bad_team_pin_falls_back(tmp_path):  # pin "4829" → 6 random digits, != "4829"
```
```go
func TestBusyPorts(t *testing.T) // hold a listener on a free port p; busyPorts([]int{p}) == []int{p}
```

- [ ] **Step 2: Run:** `python3 -m pytest tests/test_kit_pin.py -q` and `cd kit && go test -run BusyPorts ./...` → FAIL
- [ ] **Step 3: Implement.** In `runController`: right after `ensurePython`, on Windows run the firewall step (before `startLab` and before Python listens), passing `os.Executable()` and the Python path; before that, `busyPorts([]int{httpsPort, appPort, labHTTPSPort})` → `st.fail` with "Port N is used by another program (close it, or restart the laptop)" naming the owning process on Windows (`netstat -ano` → PID → `tasklist /FI "PID eq N"`).
- [ ] **Step 4: Run tests** → PASS; `go vet -tags console ./...` clean (needs `python3 kit/build.py --only linux` first for the embeds)
- [ ] **Step 5: Checkpoint**

### Task 3: One-scan phone sign-in

**Files:**
- Modify: `controller/kit.py` (new methods, `status`), `controller/server.py` (`_appapi`), `controller/web/kit.html` (QR source), `controller/web/app.html` (hash sign-in)
- Test: `tests/test_kit_phone_link.py`

**Interfaces:**
- Produces:
  - `Kit.phone_link_token() -> str`: returns the current token, minting a new `secrets.token_urlsafe(16)` when none or older than 15 min; kept in memory only
  - `Kit.redeem_phone_link(token: str) -> dict`: subscriber row of the kit household; raises `PermissionError("this sign-in link has expired: scan the QR code on the console again")` when wrong or expired; constant-time compare; 20 redemptions per token max
  - `status(local=True)` adds `"app_link_urls": [f"http://{ip}:{port}/app/#k={token}" ...]` (local only; the token never goes to LAN clients)
  - `POST /v1/app/kitlink {"token": ...}` → same session cookie as `/v1/app/login`, audit `app_login_kitlink`; only when kit mode is enabled
  - `app.html`: on load, if `location.hash` matches `#k=...`, `POST /v1/app/kitlink`, then `history.replaceState` to drop the hash, then `start()`; on 401 shows the error text on the sign-in screen

- [ ] **Step 1: Failing tests**

```python
def test_link_signs_in(client): # token from kit.phone_link_token(); POST /v1/app/kitlink → 200 and Set-Cookie iqapp=
def test_expired_link(client, monkeypatch): # advance time 16 min → 401, body contains "scan the QR code"
def test_link_not_in_lan_status(kit): # kit.status(local=False) has no "app_link_urls"
```
(Reuse the controller test fixtures already in `tests/` for a running server; follow `tests/test_controller*.py`.)

- [ ] **Step 2: Run:** `python3 -m pytest tests/test_kit_phone_link.py -q` → FAIL
- [ ] **Step 3: Implement** as above; `kit.html` draws the QR from `app_link_urls[0]` (falls back to `app_urls[0]`), the text under the QR still shows the plain address
- [ ] **Step 4: Run** the new tests and the whole suite → PASS
- [ ] **Step 5: Checkpoint**

### Task 4: Router Setup hands-off

**Files:**
- Modify: `kit/router.go` (`runPart` handlers, `installJob`), `kit/common.go` (`Profile`), `kit/web/router.html`
- Create: `kit/secret_windows.go`, `kit/secret_other.go`
- Test: `kit/router_test.go` (`//go:build router`)

**Interfaces:**
- Consumes: `loadSettings()`, `Settings.settingsJSON()` (Task 1)
- Produces:
  - `Profile.RouterPasswords map[string]string` (`json:"router_passwords,omitempty"`), host → base64 of `protect(password)`
  - `func protect(b []byte) ([]byte, error)` / `func unprotect(b []byte) ([]byte, error)`: Windows DPAPI (`windows.CryptProtectData`, current user); other OS: identity (file is 0600)
  - `func routerReady(f map[string]string, payload string, consoleHTTPS string) bool`: `f["installed"] == payload && f["agent"] == "1" && trim(f["controller"]) == trim(consoleHTTPS)`
  - `func preflight(f map[string]string) error`: python missing and `internet == "no"` → "the router has no internet connection: connect its WAN port to the internet first (it downloads Python and tc once)"; python missing and `free_kb` < 22000 → "not enough free space on the router: …"; else nil
  - `GET /api/settings` → `settingsJSON()`
  - `/api/connect` with `"UseSaved": true` and empty password tries the remembered password for that host, then `router_password` from settings; on success remembers it; on auth failure returns `{"error": ..., "need_password": true}` (no retry)
  - `/api/connect` response adds `"ready": routerReady(...)`, `"preflight": preflight(...)` (error text or "")
  - `router.html`: when settings `auto` is on: find → pick `router_address` or the single SSH candidate → connect with `UseSaved` → if `ready`: "Ready: IntentQoS <ver> is installed and paired" and stop → if `preflight` text: show it and stop → else fill plan and team PIN, 5 s countdown with Stop, install. GL.iNet acceleration warning (`⚠ MediaTek hardware NAT …`) is shown at the end as a highlighted "One step left" box with the clicks: *GL.iNet admin panel → Network → Network Acceleration → Off*.

- [ ] **Step 1: Failing tests**

```go
func TestRouterReady(t *testing.T) {
	f := map[string]string{"installed": "2.0.0", "agent": "1", "controller": "https://192.168.8.20:8443"}
	if !routerReady(f, "2.0.0", "https://192.168.8.20:8443/") { t.Fatal("same version and console must be ready") }
	f["installed"] = "1.9.0"; if routerReady(f, "2.0.0", "https://192.168.8.20:8443") { t.Fatal("older version must reinstall") }
	f["installed"] = "2.0.0"; f["controller"] = "https://10.0.0.5:8443"; if routerReady(f, "2.0.0", "https://192.168.8.20:8443") { t.Fatal("other console must re-pair") }
}
func TestPreflight(t *testing.T) // {"python":"", "internet":"no"} → error containing "no internet"; {"python":"3.11"} → nil; {"python":"", "internet":"yes", "free_kb":"9000"} → error containing "free space"
func TestProtectRoundTrip(t *testing.T) // unprotect(protect(x)) == x
```

- [ ] **Step 2: Run:** `cd kit && go test -tags router -run 'RouterReady|Preflight|Protect' ./...` → FAIL (needs the embed: build once with `python3 kit/build.py --only linux`)
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** → PASS; `go vet -tags router ./...` clean
- [ ] **Step 5: Checkpoint**

### Task 5: Test Kit hands-off and automatic failover detection

**Files:**
- Modify: `kit/testkit.go` (`runPart`, `runFailover`), `kit/web/testkit.html`
- Modify: `intentqos/daemon.py` (state snapshot) and `intentqos/api.py` only if `/api/state` lacks the WAN gateway
- Test: `kit/testkit_test.go`, `tests/test_wan_gateway_state.py`

**Interfaces:**
- Consumes: `loadSettings()` (Task 1); router `/api/state`
- Produces:
  - router state JSON key `"wan_gateway": "<ip or empty>"` (from `Daemon._wan_gateway()`, the value the estimator already tracks)
  - `func wanGateway(st map[string]any) string`
  - `runFailover(..., auto bool)` keeps its signature; while waiting for the cable it polls `/api/state` every 1 s and continues when `wanGateway` differs from the value at the start and is non-empty (the "Cable moved" button and `foSignal` stay as a fallback)
  - `GET /api/settings` → `settingsJSON()`; test ids accepted in `tests`: short names `call`, `game`, `everyone`, `rpm`, `home` map to the existing scenario ids (`call_backup`, `game_downloads`, …; see `scenarios` in `testkit.go`); unknown names → warning, skipped
  - `testkit.html` with `auto`: pair with the team PIN if not paired (retry discovery every 3 s, "Waiting for the Operator Console…") → wait until `/api/check` succeeds for the saved router ("Waiting for the router…") → 5 s countdown with Stop → run `tests` for `test_seconds` → if `failover`: failover with IntentQoS **on** (one cable move) → open the report
  - when every test's "on" median delay is not lower than its "off" median, the result screen shows: "No difference measured: check that GL.iNet Network Acceleration is off and that the plan speeds in the settings match your line" (existing `verdict` logic decides "no improvement"; this is display only)

- [ ] **Step 1: Failing tests**

```go
func TestWanGateway(t *testing.T) // {"wan_gateway":"10.42.0.1"} → "10.42.0.1"; {} → ""
func TestShortTestNames(t *testing.T) // scenarioFor("call") == "call_backup"; scenarioFor("nope") == ""
```
```python
def test_state_has_wan_gateway(): # Daemon state snapshot includes "wan_gateway" (None/"" allowed)
```

- [ ] **Step 2: Run** `cd kit && go test -tags testkit -run 'WanGateway|ShortTestNames' ./...` and the pytest → FAIL
- [ ] **Step 3: Implement** (`func scenarioFor(short string) string` in `testkit.go`)
- [ ] **Step 4: Run** → PASS, plus the existing `go test -tags testkit ./...`
- [ ] **Step 5: Checkpoint**

### Task 6: 4G Bridge hands-off and one SmartScreen warning per laptop

**Files:**
- Modify: `kit/bridge.go`, `kit/web/bridge.html`, `kit/main.go`
- Create: `kit/unblock_windows.go`, `kit/unblock_other.go`

**Interfaces:**
- Consumes: `loadSettings()` (Task 1)
- Produces:
  - `func unblockSiblings()` called first in `main()`: for every `IntentQoS *.exe` in the program's folder, `os.Remove(path + ":Zone.Identifier")` (no-op elsewhere; errors ignored, logged once)
  - `GET /api/settings` in the bridge; `bridge.html` with `auto`: polls `/api/state` every 2 s and, once a phone (USB tethering) adapter and an Ethernet adapter are both present, counts down 5 s and shares; shows "Sharing the phone's 4G" when done

- [ ] **Step 1: Test** (`kit/settings_test.go`, untagged): `TestUnblockSiblingsNoPanic` — calls `unblockSiblings()` in a temp folder with a fake exe; must not panic (behaviour itself is checked on Windows in Task 8)
- [ ] **Step 2: Run** → FAIL (undefined)
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `go test ./...` and `go vet -tags bridge ./...` → PASS
- [ ] **Step 5: Checkpoint**

### Task 7: Package, settings template, filming sheet, docs

**Files:**
- Modify: `kit/build.py` (copy two text files into `dist/kit/windows/` and write `dist/kit/IntentQoS-Kit-Windows.zip`)
- Create: `kit/dist-files/IntentQoS Kit Settings.txt` (template, `auto = yes`, `tests = call, game, everyone`, `test_seconds = 30`, `failover = yes`, `team_pin` filled with a random 6 digits at build time, password empty)
- Create: `kit/dist-files/Filming day.txt` (rehearsal, then the take, laptop by laptop)
- Modify: `docs/KIT.md` (new section "Hands-off mode for filming")

- [ ] **Step 1:** Build: `python3 kit/build.py --only windows` → the zip contains the four `.exe`, both text files
- [ ] **Step 2:** `python3 kit/build.py --only linux` → Linux builds still produced
- [ ] **Step 3: Checkpoint**

### Task 8: End-to-end checks (Linux, then Windows)

- [ ] **Step 1: Linux smoke** (`INTENTQOS_HOME` temp dir, `INTENTQOS_NO_BROWSER=1`, `INTENTQOS_SETTINGS` = a test settings file):
  start `intentqos-console --no-window` → `GET http://127.0.0.1:8088/v1/kit/where` answers and the log shows the team PIN; Test Kit's `/api/pair` with the team PIN succeeds; `POST /v1/app/kitlink` with the token from `/kit/` status (local) sets the cookie; Router Setup `/api/find` and `/api/settings` answer; bridge `/api/state` answers.
- [ ] **Step 2: Full suites:** `python3 -m pytest -q` → all pass; `go test` for untagged, `-tags testkit`, `-tags router` → all pass; `go vet` for all four tags → clean
- [ ] **Step 3: Windows** (user approved; linked PC desktop-s6hcfak): copy the zip's contents to a granted test folder, start each `.exe` with `INTENTQOS_HOME` pointed at that folder, check each window's local API answers and each log has no error; console run with `--no-window` and firewall step skipped for the local-only check (no system changes)
- [ ] **Step 4:** Deliver the zip and the filming sheet to the user
