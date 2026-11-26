# Home kit: hands-off mode for filming the Round 2 video

## Why

The team (Mistral Knights) will film a 3-minute video for TELIPORT Round 2 that shows IntentQoS working on real
hardware on their own home line. Every prompt, typed field or error on camera costs a retake. The kit programs
already work (Linux builds passed the full-system emulation on real OpenWrt firmware), but on Windows each one
stops several times for input, and the Windows builds have never been run end to end.

Goal: once the cables are in, each Windows laptop does its part with one double-click and runs by itself, and the
whole run can be repeated for retakes without reinstalling anything.

## Goals

1. One double-click per laptop on the day; no typing during the take.
2. No Windows prompt during the take: the one-time prompts (SmartScreen, firewall, administrator) happen in a
   rehearsal run before filming.
3. Nothing in the take can end on an error screen that a check before the take could have caught; when something
   is wrong, the program says what to do in plain words.
4. Retakes: running everything again is safe and quick (an installed, paired router is not reinstalled).
5. Verified on Windows, not only on Linux.

## Non-goals

* No new "director" program and no rewrite. The existing programs (Operator Console, Router Setup, Test Kit,
  4G Bridge, phone app) gain a hands-off mode and fewer stops.
* No code signing (needs a paid certificate). The SmartScreen warning is reduced to one per laptop, done in rehearsal.
* No change to the engine's algorithms or the measured results.

## Anti-goals (failure even if it technically works)

* Anything that can break the install path that already works (interactive mode must keep working as today).
* Secrets committed to the repository.
* A prompt, a typed field or a "press OK" appearing in the middle of a take.

## Constraints

* All laptops are Windows 10/11. Router: GL.iNet GL-MT3000 (GL firmware or upstream OpenWrt) or a Cudy WR3000
  with OpenWrt. The router needs internet during the first install (packages from its feed).
* Programs are unsigned Go executables; the console carries its own Python.
* The user does not want commits or pushes unless asked.

## Approaches considered

1. **Hands-off mode in the existing programs** (chosen). A settings file next to the programs presets everything;
   each program reads it and proceeds by itself; a rehearsal run clears the one-time Windows prompts. Lowest risk:
   same programs, fewer stops.
2. One director program that drives the whole demo and shows one dashboard. Slickest on camera, but a lot of new
   code a week before filming, close to the rewrite the team rejected, and new ways to fail.
3. A batch file per laptop calling the existing command-line modes. Least work, but black command windows on video
   and poor recovery from a hiccup mid-take.

The team left the choice to us ("whatever is best to show in the video"); approach 1 it is.

## Design

### The settings file (`IntentQoS Kit Settings.txt`, next to the programs)

Plain `key = value` lines with comments, written by the team once and copied to every laptop with the programs.
Every key is optional; a missing key means today's behaviour (the program asks).

```
team_pin        = 482913      # same 6 digits on every laptop: the console uses it, the others pair with it
router_address  = 192.168.8.1 # optional; found automatically otherwise
router_password =             # optional; Router Setup also remembers it after the first sign-in
plan_down       = 100         # your plan, Mbit/s
plan_up         = 100
auto            = yes         # programs start their job by themselves (5 s countdown with a Stop button)
tests           = call, game, everyone   # Test Kit tests, in order
test_seconds    = 30
failover        = yes         # include the fibre -> 4G test
```

* A fixed team PIN replaces the random per-start PIN when present; the 5-wrong-PINs lockout stays.
* Router Setup also remembers a router's password on that laptop after the first successful sign-in
  (Windows DPAPI, current user), so the file does not have to carry it.

### Per program

* **Operator Console.** Uses `team_pin`. Phone QR code becomes a one-scan sign-in link (short-lived, single
  household), so the phone opens straight into the app: no number, no code. Shows "Ready" with the checklist.
* **Router Setup.** On open, in hands-off mode: finds the router (or uses `router_address`), signs in with the
  remembered or preset password, and checks what is there. Already installed with this version and paired with this
  console: shows **Ready** and stops (no reinstall; retakes are instant). Otherwise counts down 5 s and installs and
  pairs with the team PIN, no typing. If the router has GL.iNet hardware acceleration loaded, it is shown as the one
  remaining step with the exact clicks.
* **Test Kit.** Pairs with the team PIN, waits until the router is reporting, counts down 5 s, runs `tests` with
  IntentQoS off then on, live chart on screen, report opens at the end. Failover: it asks for the cable move and
  then **detects** the move itself from the router's new WAN gateway, so nobody has to press "Cable moved" on camera
  (the button stays as a fallback).
* **4G Bridge.** Hands-off: waits for the phone's USB tethering and shares it as soon as the phone appears.
* **Phone.** Scan the QR once; Add to Home screen (a browser step that cannot be automated).

### One-time prompts, done in rehearsal

* The first kit program opened on a laptop removes the downloaded-file mark from the other kit programs in the
  same folder, so Windows' SmartScreen warning appears once per laptop, not once per program.
* The console's existing one administrator prompt also adds firewall rules for its own program and its Python
  (not only ports), and removes any "block" rule Windows made if someone earlier pressed Cancel on the
  "allow Python" prompt. After the rehearsal no firewall prompt can appear.

### Checks before anything can fail

* Console: ports 8443/8088/8446/8447 in use by another program → says which program, before starting.
* Router Setup: router has no internet / too little flash / wrong password → plain message and the fix, before
  installing. (These exist in the installer; surface them before the upload.)
* Test Kit: router not reporting yet → waits with a clear status instead of failing; a test that shows no
  difference → suggests the acceleration setting and the plan speeds.

### Verification

* Go and Python test suites stay green; new unit tests for the settings file, team PIN, one-scan link and the
  Router Setup "already installed" decision.
* Linux smoke run of every program end to end against each other (console + pairing + Test Kit + phone link).
* Windows: build the .exe files and run them on the team's Windows PC through the linked computer (with the team's
  OK), checking each starts, pairs locally and logs no errors.

### Delivered

A `IntentQoS Kit` folder (zip) with the four programs, the settings file filled with safe defaults, and a one-page
"Filming day" sheet: rehearsal steps, then the take, laptop by laptop. Docs (`docs/KIT.md`) updated.

## Left to the builder

* Exact UI wording and layout of the countdowns and "Ready" states.
* How the router reports its WAN gateway to the Test Kit (add it to `/api/state` if it is not already there).
* Order of work and how the Windows run is driven.
