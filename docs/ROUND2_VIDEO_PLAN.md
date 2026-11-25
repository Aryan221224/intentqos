# Round 2 video: full design (not produced yet)

TELIPORT Season 4, Case Study 3 (Adaptive QoS Engine for Mixed Home Broadband Traffic). This is the complete plan for
the 3-minute video: what the organiser requires, what the judges score, the timeline, every clip to record and how,
the presenter script word for word, the overlays, and the export settings. Nothing here has been recorded yet.

---

## 1. What the organiser requires (from the Round 2 mail and the website guidelines)

| Requirement | Source | How this plan meets it |
|---|---|---|
| A **3-minute video presentation**, submitted through the website, together with a 10-slide deck | Round 2 mail | Cut to **2:55–2:58**. Never over 3:00. |
| **Case Study 3: "Build and demonstrate your solution on Linux platform"** | Round 2 mail | Every live shot is the real engine on **Ubuntu 24.04**, with a permanent on-screen badge saying so. No Windows windows appear on screen. |
| **MP4** format, **not more than 50 MB** | Website guidelines | H.264 + AAC MP4, two-pass encode to about 42 MB (section 8). |
| **"Video should include both the solution as well as the presenter (as per this sample)"** | Website guidelines, sample frame | The sample frame (1280×720) has the solution full-screen and the presenter in a box at the **bottom-right**. This layout is used for **the whole video**, title and end card included (section 3). |
| Supporting sheets and details go in the **annexures** | Website guidelines | The video does not try to show everything; it points to the deck's appendix and the repository for full results. |
| File naming: **LeaderFirstName_TeamName_InstituteName** | Website guidelines | Apply to the video file too: `LeaderFirstName_TeamName_InstituteName.mp4`. |
| Only the team leader uploads; the submission can be changed until the deadline | Website guidelines | Upload a complete early version, then replace it if the final cut improves. |

What the case-study brief asks the solution to prove (the judges will look for each of these):

- Three named scenarios: **an ISO download during a video conference**, **the WAN dropping from 100 to 20 Mbit/s**,
  and **three TVs streaming while a game needs low latency**.
- A **temporary user intent** ("prioritise my work call") that **keeps fairness**.
- Classification **without reading payload**, with **confidence** and **correction**.
- Where AI is used, a **comparison with a deterministic baseline** showing a measurable improvement.
- Remediation that is **bounded, observable and reversible**, returning to a **known safe state** on failure.
- **Automated baseline-vs-optimised experiments** (reset, inject, collect, report).
- A dashboard with **latency, jitter, loss, throughput, queue depth and fairness**.
- **Emulated capabilities labelled as emulation.** No media used against its licence.
- Scoring: classification and link-estimation accuracy; latency, jitter and loss reduction; fairness and no
  starvation; adaptation speed and stability; **CPU overhead**, reproducibility and clarity.

## 2. What will make it win

1. **Show, then prove.** Each scenario is shown live on Linux (the judges see it happen), followed by a 4–5 second
   result card from the seeded experiments (the judges see it is not a lucky take).
2. **Hit every brief scenario by name.** On-screen labels "Scenario 1 of 3 (from the brief)" make the mapping
   obvious to an evaluator ticking a checklist.
3. **AI with a baseline, and honesty.** Say where the model helps and the one place rules still win. Tata Elxsi frames
   TELIPORT as "AI-first"; the brief warns against AI that is only decoration. Measured, honest AI is the answer to
   both.
4. **Speak the company's language.** Tata Elxsi's broadband teams build RDK-B gateways for operators, including QoS,
   5G fixed-wireless and AI-driven CPE QoS monitoring (iCX). Show IntentQoS as an operator product: gateway package,
   fleet controller, subscriber app, TR-181 data model, staged signed rollouts.
5. **Calm, clear, exact.** One idea per segment, numbers on screen as you say them, no claims beyond the evidence.

## 3. Frame layout (from the sample)

Measured from the sample frame (1280×720):

| | Sample (1280×720) | Recommended master (1920×1080) |
|---|---|---|
| Presenter box | x 955–1257, y 421–703: **302 × 282 px** | x 1433–1886, y 632–1055: **453 × 423 px** |
| Right margin / bottom margin | 23 px / 17 px | 34 px / 25 px |
| Share of the frame | 24 % of the width, 39 % of the height | same |

- **Presenter box:** head and shoulders, centred, eyes about one third from the top, plain uncluttered background,
  even light on the face. A thin white or brand-colour border (2–3 px) separates it from the content. A small name
  tag inside the box at the bottom (as the sample's "Presenter" label): `<First name> · <Team name>`.
- **Keep-clear zone:** nothing important in the right 26 % × bottom 42 % of the frame. Before recording each screen
  clip, check that the console's numbers, buttons and charts sit outside that rectangle (use browser zoom and window
  size; see section 6). Result cards and labels go top-left or top-centre.
- **Permanent badge, top-left, small** (the brief's emulation rule):
  `LIVE on Linux · Ubuntu 24.04 · emulated home network and ISP line`. On stills from the repository use
  `Recorded result` instead of `LIVE`.
- **Presenter is visible from the first frame to the last.** Do not switch to a full-screen presenter: the guideline
  asks for "as per this sample".

## 4. Timeline (2:57 total)

| # | Time | Length | Segment | Brief items covered |
|---|---|---|---|---|
| 0 | 0:00–0:09 | 9 s | Hook and title | problem statement |
| 1 | 0:09–0:26 | 17 s | What we built: architecture | architecture diagram, no payload, Linux tc |
| 2 | 0:26–0:50 | 24 s | Scenario 1: ISO download during a video call | scenario 1, dashboard, throughput kept |
| 3 | 0:50–1:13 | 23 s | Scenario 2: line drops 100 → 20 Mbit/s | scenario 2, link estimation, adaptation speed |
| 4 | 1:13–1:37 | 24 s | Scenario 3: three TVs + game; classifier, confidence, correction, AI vs rules | scenario 3, classification, AI vs baseline |
| 5 | 1:37–1:56 | 19 s | Temporary intent from the phone, with fairness | intent API/UI, fairness, no starvation |
| 6 | 1:56–2:09 | 13 s | Safety: crash the engine, watchdog, rollback | bounded, observable, reversible, safe state |
| 7 | 2:09–2:32 | 23 s | Proof: automated experiments, metrics, CPU, real firmware | experiments, reproducibility, CPU overhead |
| 8 | 2:32–2:48 | 16 s | Product path for an operator | production boundary, operator relevance |
| 9 | 2:48–2:57 | 9 s | Close and honest boundary | known limitations |

Spoken words: about 400 at a calm 140–145 words a minute, which leaves pauses for the visuals to land.

## 5. Shot-by-shot design with the presenter script

Conventions: **[SCREEN]** what fills the frame, **[CLIP]** the recording it comes from (section 6), **[ON SCREEN]**
overlays added in the edit, **[SAY]** the presenter's words. Numbers in result cards come only from the committed
results; the sources are listed in section 9.

### Segment 0 · 0:00–0:09 · Hook and title

- [SCREEN] 0:00–0:04: clip C1 (Monitor view, calm home, call running, grade A).
  0:04–0:09: title card fades in over a dimmed C1.
- [ON SCREEN] Title card:
  **IntentQoS** · *Intent-aware adaptive QoS for mixed home broadband traffic*
  `Tata Elxsi TELIPORT Season 4 · Case Study 3 · <Team name>, <Institute>`
- [SAY] (20 words)
  > "Evening, in an Indian home. A work call, three TVs, a game... and someone starts a big download. The call freezes."

### Segment 1 · 0:09–0:26 · What we built

- [SCREEN] Still S1 (`docs/img/architecture.png`), with a slow zoom (Ken Burns, 105 % → 112 %). Highlight boxes appear
  in order as they are named: the router engine, Linux tc and nftables, the dashboard.
- [ON SCREEN] Three short tags, top-left, appearing in turn:
  `Header-only: no payload, nothing decrypted` · `Measures real line capacity` · `Linux tc + nftables, every 0.5 s`
- [SAY] (46 words)
  > "The cause is queues, and rules that can't tell what matters. So we built IntentQoS. It runs on the home router,
  > recognises traffic from packet sizes and timing only, never the payload, measures the line's real capacity, and
  > re-tunes Linux traffic control every half second."

### Segment 2 · 0:26–0:50 · Scenario 1: ISO download during a video call

- [SCREEN] 0:26–0:44: clip C2. Gateway console, **Monitor** view, call running. The cursor clicks *ISO download on the desktop* and
  *Cloud backup from the NAS* under **Experiments → Inject conditions by hand**, then returns to **Monitor**: bulk strips fill,
  the voice and video strips' queue stays near zero, the delay chart stays flat, the grade stays A.
  0:44–0:50: result card R1 over the end of C2.
- [ON SCREEN] Label, top-left: `Scenario 1 of 3 (from the brief): ISO download during a video call`.
  Arrow callouts for 2 s each while speaking: `call delay`, `queue per class`, `download throughput`.
  Result card R1: `Call delay p99 (upload): 113 ms → 17 ms` · `ISO download still 79 Mbit/s` ·
  footnote `10 seeded runs per arm, emulated 100/20 Mbit/s line (results/REPORT.md)`.
- [SAY] (48 words)
  > "Here it is, live on Linux. A video call is running. Now an ISO download and a cloud backup start. The bulk queues
  > fill, but the call's delay stays flat, and the download still gets its share. Over ten seeded runs, call delay
  > fell from 113 to 17 milliseconds."

### Segment 3 · 0:50–1:13 · Scenario 2: the line drops from 100 to 20 Mbit/s

- [SCREEN] 0:50–1:08: clip C3. **Link** view. The cursor clicks **Drop to 20/5**. The delay chart spikes, then the
  shaper's rate steps down and the delay settles; cut to **Policy** for 3 s showing the logged reason
  (`bufferbloat (+N ms) ... rate ... -> ...`).
  1:08–1:13: result card R2.
- [ON SCREEN] Label: `Scenario 2 of 3: WAN drops 100 → 20 Mbit/s (emulated ISP line)`.
  Callout on the Policy line: `every change logged with its reason`.
  Result card R2: `Call delay p99 once settled: 219 ms → 47 ms` · `Call quality (MOS): 2.24 → 3.90` ·
  `Back within 30 ms in ~5 s` · footnote as R1.
- [SAY] (47 words)
  > "Now the ISP silently drops the line from 100 to 20 megabits. Nobody tells the router. The estimator sees delay
  > rising under load, recalculates the shaper within about a second, and logs why. Without QoS the call scores 2.2;
  > with IntentQoS, 3.9."

### Segment 4 · 1:13–1:37 · Scenario 3: three TVs and a game; classifier vs rules

- [SCREEN] 1:13–1:28: clip C4. Inject *Three TVs streaming* and *Online game on the console*, then **Traffic** view: each flow with its class,
  confidence and source. The cursor opens one flow's menu and corrects its class (one click), the row updates.
  1:28–1:37: result card R3 (AI vs rules).
- [ON SCREEN] Label: `Scenario 3 of 3: three TVs streaming + a game`.
  Callouts: `class + confidence`, `abstains when unsure`, `one-click correction`.
  Result card R3, a small two-column table titled `ML classifier vs our deterministic rules`:
  `TV rebuffering after the line drop: 18 s → 3.1 s` · `Web page load during a download (p95): 142 ms → 76 ms` ·
  `Game delay p99 in scenario 3: rules 27 ms, ML 45 ms (rules win here)`.
- [SAY] (54 words)
  > "Three TVs and a game. Every flow gets a class and a confidence; when the model is unsure, it abstains, and one
  > click corrects it. Against our own rule baseline, TVs rebuffer three seconds instead of eighteen, and pages load
  > in 76 instead of 142 milliseconds. Rules still win on game delay, and we report that."

### Segment 5 · 1:37–1:56 · Temporary intent, with fairness

- [SCREEN] 1:37–1:50: clip C5. The subscriber app in a phone-sized window on the left half: **Boost** → *Asha's laptop*
  → *Calls and games* → *1 hour* → the impact preview → confirm → Home shows the countdown.
  1:50–1:56: clip C5b. Gateway **Prioritise** view with the lease and its countdown; cut to **Monitor**: bulk still
  moving above its floor.
- [ON SCREEN] Label: `Temporary intent: "prioritise my work call for an hour"`.
  Tags: `ends by itself` · `daily budget` · `all boosts ≤ 60 % of the line` · `every class keeps a floor`.
- [SAY] (44 words)
  > "Asha has a board meeting. From her phone she prioritises her laptop for one hour. She sees the impact first. The
  > boost is budgeted, all boosts together are capped at sixty percent of the line, every class keeps a floor, and
  > it ends by itself."

### Segment 6 · 1:56–2:09 · Safety

- [SCREEN] clip C6. **Experiments → Crash the engine** → confirm. The red banner appears, traffic keeps flowing, the
  watchdog line appears (`restored safe ... in N ms`), then **Restart engine**.
- [ON SCREEN] Label: `Fault injection: engine killed with SIGKILL`.
  Tag: `independent watchdog · known safe state · median 1.8 s over 10 runs`.
- [SAY] (27 words)
  > "And if the engine crashes, an independent watchdog restores a known safe state in under two seconds. Every change
  > is bounded, read back, and reversible."

### Segment 7 · 2:09–2:32 · Proof: experiments, metrics, CPU, real firmware

- [SCREEN] 2:09–2:19: clip C7, sped up ×8: a terminal running the scripted experiment (reset, inject, collect), then
  the generated report (`results/report.html` or **Experiments** with medians, spread, p-values and a figure).
  2:19–2:25: clip C8, **System** view: **Setup check** all green, `engine CPU` about 2 %.
  2:25–2:32: still S2, `results-systemsim/screens/14-router-setup-5-result.jpg` (Router Setup finished on OpenWrt),
  then S3 `results-systemsim/charts/router_cpu.png` for 2 s.
- [ON SCREEN] `Sped up ×8` badge on C7. Tags: `no QoS · static QoS · rules · ML` · `10 seeds per arm` ·
  `engine ≈ 2 % of one PC core under a full household load` ·
  `Real OpenWrt 25.12 firmware in emulation: 8 bugs found and fixed`.
- [SAY] (49 words)
  > "One command resets the lab, injects each scenario against no QoS, static QoS and our rules, collects the evidence,
  > and writes the report. Under a full household load the engine uses about two percent of a PC core. On real
  > OpenWrt firmware, emulated, testing exposed eight bugs, all fixed."

### Segment 8 · 2:32–2:48 · The product an operator would ship

- [SCREEN] 2:32–2:40: still S4 `docs/img/ops-overview.png` (operator console, fleet overview).
  2:40–2:44: still S5 `docs/img/app-home-hi.png` (subscriber app in Hindi).
  2:44–2:48: still S6 `docs/img/kit-hardware-setup.png` (gateway package on a GL.iNet OpenWrt router).
  Optional instead of S6, if you have the router: a 4 s real shot of the GL-MT3000 on the desk (clip H1).
- [ON SCREEN] Tags: `OpenWrt package` · `fleet controller: signed, staged rollouts` · `TR-181 data model, USP-style
  agent` · `English and हिन्दी`. Footnote on S4: `1 real gateway + 240 simulated homes`.
- [SAY] (38 words)
  > "And it's built the way an operator would ship it: a router package, a fleet controller with signed, staged
  > rollouts, a subscriber app in English and Hindi, and a TR-181 data model, the language of RDK-B and prpl gateways."

### Segment 9 · 2:48–2:57 · Close

- [SCREEN] End card over a dimmed C1: `IntentQoS` · `Calls stay smooth. Downloads keep moving.` ·
  `Emulated conditions are labelled; limits documented; physical-router trial next` · `<Team name> · <Institute>` ·
  the repository link if it is public.
- [SAY] (22 words)
  > "Everything emulated is labelled, and every limit is documented. IntentQoS: calls stay smooth, downloads keep
  > moving. Thank you."

### Full presenter script in one block (for the teleprompter)

> Evening, in an Indian home. A work call, three TVs, a game... and someone starts a big download. The call freezes.
>
> The cause is queues, and rules that can't tell what matters. So we built IntentQoS. It runs on the home router,
> recognises traffic from packet sizes and timing only, never the payload, measures the line's real capacity, and
> re-tunes Linux traffic control every half second.
>
> Here it is, live on Linux. A video call is running. Now an ISO download and a cloud backup start. The bulk queues
> fill, but the call's delay stays flat, and the download still gets its share. Over ten seeded runs, call delay fell
> from 113 to 17 milliseconds.
>
> Now the ISP silently drops the line from 100 to 20 megabits. Nobody tells the router. The estimator sees delay
> rising under load, recalculates the shaper within about a second, and logs why. Without QoS the call scores 2.2;
> with IntentQoS, 3.9.
>
> Three TVs and a game. Every flow gets a class and a confidence; when the model is unsure, it abstains, and one
> click corrects it. Against our own rule baseline, TVs rebuffer three seconds instead of eighteen, and pages load in
> 76 instead of 142 milliseconds. Rules still win on game delay, and we report that.
>
> Asha has a board meeting. From her phone she prioritises her laptop for one hour. She sees the impact first. The
> boost is budgeted, all boosts together are capped at sixty percent of the line, every class keeps a floor, and it
> ends by itself.
>
> And if the engine crashes, an independent watchdog restores a known safe state in under two seconds. Every change is
> bounded, read back, and reversible.
>
> One command resets the lab, injects each scenario against no QoS, static QoS and our rules, collects the evidence,
> and writes the report. Under a full household load the engine uses about two percent of a PC core. On real OpenWrt
> firmware, emulated, testing exposed eight bugs, all fixed.
>
> And it's built the way an operator would ship it: a router package, a fleet controller with signed, staged
> rollouts, a subscriber app in English and Hindi, and a TR-181 data model, the language of RDK-B and prpl gateways.
>
> Everything emulated is labelled, and every limit is documented. IntentQoS: calls stay smooth, downloads keep moving.
> Thank you.

(About 400 words. Read it aloud with a stopwatch: it should take 2:40–2:48, leaving room for the pauses marked by the
segment cuts. If it runs long, cut in this order: the last sentence of segment 6, "and logs why" in segment 3, "the
language of RDK-B and prpl gateways" in segment 8.)

## 5b. Who records what

| Who | What |
|---|---|
| **Claude (this cloud session)** | All screen clips C1–C8, recorded from the real lab on Ubuntu 24.04 here; the stills; title, end card, labels and result cards; a **guide video** (the locked screen cut with the script as captions and a countdown per segment) for the presenter to record against; the final composite, captions, audio clean-up and the <50 MB export. |
| **The team** | The presenter footage (P1), room tone (P2), the team details (T1), and optionally the router shot (H1). Specification in section 7b. |

## 6. Clips to record

### Recording machine and settings

- **Ubuntu 24.04** on the laptop (native or dual boot; a VM also works). Not WSL: the brief says Linux and the
  footage must look like it.
- Screen **1920×1080**, 30 fps, OBS Studio (`sudo apt install obs-studio`), output MKV, CQP/CRF 18 (edit-quality;
  compression happens at export). Record the screen **without** the presenter; the presenter is composited later.
- Browser: Chromium app windows (`chromium --app=http://localhost:8090/`), no tabs or bookmarks bar, notifications
  off (Do Not Disturb), clean desktop, cursor highlight on (OBS cursor-highlight filter or the GNOME setting), zoom
  **110–125 %** so numbers are readable in a 1080p frame.
- **Before each clip, check the keep-clear zone** (right 26 % × bottom 42%): resize the window or scroll so nothing
  you talk about sits there. A transparent overlay of the presenter box on the monitor (a PNG on top in OBS's
  preview) makes this easy.
- Start the lab: `sudo scripts/stack.sh up`, then open `http://localhost:8090/` (gateway console),
  `http://localhost:8088/app/` (subscriber app, narrow window) and `http://localhost:8088/ops/` (operator console).
  The lab starts a call and browsing by itself. Check **System → Setup check** is green and the grade is A first.
- Between clips, reset the lab: **Experiments → Stop all traffic** (stops every preset and puts the link back at
  100/20), then start only what the next clip needs. The preset switches are named *Video call on the laptop*,
  *ISO download on the desktop*, *Cloud backup from the NAS*, *Three TVs streaming*, *Online game on the console*,
  *Browsing on the phone*.
- Record **each clip 2–3 times**; keep the cleanest take. Raw length is longer than used length on purpose.

### Clip list

| ID | Used in | Used length | Raw to record | What to record, exactly |
|---|---|---|---|---|
| **C1** | 0, 9 | 9 s + 9 s | 40 s | **Monitor** view, calm home: only the call and browsing running, grade A, delay chart flat. No clicks. Slow and still. |
| **C2** | 2 | 18 s | 60 s | Start on **Monitor** with the call running. Go to **Experiments → Inject conditions by hand**, click *ISO download on the desktop*, then *Cloud backup from the NAS*. Return to **Monitor** and hold 30 s: bulk and best-effort strips fill, voice and video stay near "no queue", the delay chart stays flat, the grade stays A. Move the cursor slowly to the call's strip, then the bulk strip, then the throughput numbers (for the callouts). |
| **C3** | 3 | 18 s | 60 s | Call, one TV and browsing running. **Link** view. Click **Drop to 20/5**. Hold 25 s: spike, the rate steps down, the delay settles. Then open **Policy** and hover the newest estimator line with its reason. Afterwards restore the link (**Experiments → Stop all traffic**). |
| **C4** | 4 | 15 s | 60 s | Inject *Three TVs streaming* and *Online game on the console*. Wait 20 s for classes to settle. **Traffic** view: hover a TV flow (class *streaming*, confidence, source *ml*), then a game flow. Open one flow's menu, choose a different class, show the row update and the "user" source. Then undo the correction. |
| **C5** | 5 | 13 s | 45 s | Subscriber app in a phone-sized window (about 420×860) placed on the **left half**: **Boost** → *Asha's laptop* (or the device named *Work laptop*) → *Calls and games* → *1 hour* → impact preview (hold 3 s) → confirm → Home with the countdown and *Working*. |
| **C5b** | 5 | 6 s | 30 s | Gateway **Prioritise** view showing the same lease with its countdown; then **Monitor** with the download and backup still moving above their floors. |
| **C6** | 6 | 13 s | 40 s | **Experiments → Crash the engine** → confirm. Hold until the red banner and the watchdog's "restored safe" event appear (about 2 s) and the traffic keeps flowing; then **Restart engine**. If you prefer the terminal: `sudo pkill -9 -f intentqos.daemon` in a terminal placed top-left, the console beside it. |
| **C7** | 7 | 10 s (sped up ×8 from ~80 s) | 2–3 min | Terminal (dark theme, font 18 pt, top-left of the screen): `sudo python3 -m testbed.run_eval --reuse-testbed --scenarios s1_iso_during_call --arms noqos,static,adaptive_rules,adaptive_ml --seeds 1 --out /tmp/demo-eval` (stop the stack's lab engine first, or run it from the console's **Experiments** view: run S1 with all arms), then `make report` or open the **Experiments** results with medians, p-values and a figure. |
| **C8** | 7 | 6 s | 20 s | **System** view: scroll to **Setup check** (all green) and the `engine CPU (one core = 100%)` tile while the full household is running (all presets on). |
| **H1** *(optional)* | 8 | 4 s | 20 s | Only if you have the GL.iNet router: a steady phone shot, landscape, of the router on the desk with its cables in, status light on. No faces, no screens with personal data. |

### Stills from the repository (already exist, no recording needed)

| ID | File | Used in |
|---|---|---|
| S1 | `docs/img/architecture.png` | 1 |
| S2 | `results-systemsim/screens/14-router-setup-5-result.jpg` | 7 |
| S3 | `results-systemsim/charts/router_cpu.png` | 7 |
| S4 | `docs/img/ops-overview.png` | 8 |
| S5 | `docs/img/app-home-hi.png` | 8 |
| S6 | `docs/img/kit-hardware-setup.png` | 8 |
| Backup | `docs/console-replay.html`, `docs/img/gateway-*.png`, `results/fig_s1.png`, `fig_s2.png`, `fig_s3.png` | replace any live clip that fails |

### Result cards to design (same style as the deck)

| ID | Lines | Source |
|---|---|---|
| R1 | Call delay p99 (upload) 113 → 17 ms; ISO download 79 Mbit/s | `results/REPORT.md`, S1, no QoS vs ML, medians of 10 seeds |
| R2 | Call delay p99 once settled 219 → 47 ms; MOS 2.24 → 3.90; back within 30 ms in 4.9 s | `results/REPORT.md`, S2 |
| R3 | TV rebuffering after the drop 18 s (rules) → 3.1 s (ML); page load p95 142 → 76 ms; game delay rules 27 ms vs ML 45 ms | `results/REPORT.md`, S2, S1 and S3 |

Card style: dark translucent panel, top-left or top-centre, large numbers in the brand accent colour, a one-line grey
footnote: `Emulated 100/20 Mbit/s line · medians of 10 seeded runs per arm`. On screen 4–5 s each; never under the
presenter box.

## 7. Presenter recording

- **Who:** one presenter for the whole video (the team leader or the clearest speaker). Other members can be named on
  the end card. One voice keeps 3 minutes coherent.
- **How:** record after the screen edit is locked. Play the edited screen timeline on a second screen or laptop and
  speak the script to it, so the words land on the right visuals. Record 2–3 full takes; pick the best per segment.
- **Camera:** phone or webcam at eye level, landscape, 1080p, 30 fps. Frame head and shoulders with space above the
  head; the final crop is close to square (302×282 in the sample), so stay centred.
- **Background:** plain wall or a tidy room; nothing moving; no bright window behind you.
- **Light:** face lit from the front (a window in front of you or a lamp behind the camera).
- **Sound:** a clip-on mic or wired earphone mic, quiet room, soft furnishings around. Record a 5-second silence at
  the start for noise reduction. Audio quality matters more than video quality.
- **Delivery:** calm, about 140 words a minute, look at the lens, small natural gestures inside the frame. Smile on
  the first and last lines.
- **Dress:** plain, solid colour (no fine stripes or logos), contrasting with the background.

## 7b. What the team sends (exact specification)

| ID | What | How | Length |
|---|---|---|---|
| **P1** | The presenter speaking the script, **one file per segment** (10 segments, 0–9), 2 takes each | Play the guide video on a laptop beside the camera and speak each segment as its captions appear. Phone in landscape on a stand at eye level, **1920×1080, 30 fps, HDR off**, exposure and focus locked. Head and shoulders, centred, space above the head (the final crop is close to square). Plain background, face lit from the front, clip-on or earphone mic. Hold a natural smile for 2 s before the first word of segment 0 and after the last word of segment 9. | about 3 min of speech, 6–8 min of files |
| **P2** | Room tone | Same room, same mic, everyone silent | 10 s |
| **T1** | Team details | Team name, institute name, team leader's first name (for the file name), presenter's name tag text, team members for the end card, the submission deadline | text |
| **H1** *(optional)* | The GL.iNet router on the desk | Only if you have it: landscape, 1080p 30 fps, steady, router with cables in and lights on, no faces or personal screens | 20 s |

File names: `P1-seg03-take2.mp4`, `P2-roomtone.mp4`, `H1-router.mp4`. Do not edit, trim, add filters or compress
them. Send the original files (a shared Google Drive folder works; name the folder in the chat).

## 8. Edit and export

- **Editor:** Kdenlive or Shotcut (both free on Linux), or DaVinci Resolve. Timeline 1920×1080, 30 fps.
- **Tracks:** V1 screen clips and stills; V2 result cards, labels and badges; V3 presenter (scaled to 453×423,
  positioned x 1433, y 632, 2–3 px border, name tag); A1 presenter voice; no other audio.
- **Music:** none, or a very quiet royalty-free bed with a licence you can name. The brief forbids media used against
  its licence; no film, TV or YouTube clips anywhere (the lab's TVs are synthetic traffic, which is fine).
- **Transitions:** straight cuts or 6-frame cross-fades. No flashy effects.
- **Captions:** burned-in subtitles of the script (bottom-left, outside the presenter box) help judges who watch muted.
  Keep them to two lines.
- **Audio:** noise reduction, then normalise to about −16 LUFS. Remove breaths and long pauses.
- **Export the master** at high quality, then compress to the limit with a two-pass encode (target about 42 MB for
  2:57):

```bash
ffmpeg -y -i master.mov -c:v libx264 -preset slow -b:v 1750k -pass 1 -an -f mp4 /dev/null
```

```bash
ffmpeg -i master.mov -c:v libx264 -preset slow -b:v 1750k -pass 2 -c:a aac -b:a 128k -ar 48000 -movflags +faststart LeaderFirstName_TeamName_InstituteName.mp4
```

```bash
ffprobe -v error -show_entries format=duration,size -of default=nw=1 LeaderFirstName_TeamName_InstituteName.mp4
```

The last command must show a duration under 180 s and a size under 52428800 bytes (50 MB). If you prefer the 1280×720
size of the sample frame, add `-vf scale=1280:720` and use `-b:v 1500k`.

## 9. Claims and their evidence (say nothing that is not on this list)

| Claim in the script | Evidence |
|---|---|
| Header-only, no payload read | `docs/MODULES.md` §1 privacy boundary; unit test that changes payload bytes |
| Re-tunes every half second | `WINDOW_S` policy step, 0.5 s |
| S1: call delay p99 113 → 17 ms; ISO 79 Mbit/s | `results/REPORT.md`, README results table |
| S2: recalculates within about a second; MOS 2.24 → 3.90; back within 30 ms in 4.9 s | `results/REPORT.md`; `docs/JUDGES.md` |
| Classifier abstains when unsure; one-click correction | conformal prediction sets; `POST /api/flows/label` and the Traffic view |
| TV rebuffering 18 → 3.1 s (after the drop); page load 142 → 76 ms; rules win game delay (27 vs 45 ms) | README "Does the ML help?" and results table |
| Intent: budgeted, ≤ 60 % of the line, floors, expires | `intentqos/config.py` (`max_boost_fraction 0.60`, `daily_budget_min`, `max_duration_s`); policy floors |
| Watchdog: safe state in under two seconds | S5: median 1.8 s over 10 runs (2 s heartbeat timeout in the lab) |
| One command: reset, inject, collect, report | `testbed/run_eval.py`, `make eval report` |
| Engine ≈ 2 % of one PC core under a full household load | `docs/ROUTER_PROFILE.md`, CPU section (1.9–2.3 %) |
| Real OpenWrt firmware in emulation, 8 bugs found and fixed | `results-systemsim/REPORT.md` |
| Router package, fleet controller with signed staged rollouts, app in English and Hindi, TR-181 data model | `router/`, `controller/`, `docs/CONTROLLER.md`, `intentqos/usp.py`, `docs/img/app-home-hi.png` |

Do **not** say: that it was tested on a physical router or a real ISP line (it was not); that IPv6 traffic was shaped
live (not yet); that it runs on RDK-B (it speaks TR-181; it is packaged for OpenWrt); any router CPU percentage.

## 10. Production schedule (three short days)

| Day | Work |
|---|---|
| 1 | Install Ubuntu tools, start the lab, rehearse every clip twice, set browser zoom and window positions against the keep-clear overlay. Design the title card, end card, labels and result cards (match the deck). |
| 2 | Record clips C1–C8 (and H1 if available), 2–3 takes each. Rough-cut the screen timeline to the section 4 timings. |
| 3 | Record the presenter against the locked screen cut. Final edit, captions, audio clean-up, export, size check, watch it end to end on a phone and a laptop. Upload early; replace later if you improve it. |

## 11. Final checklist before upload

- [ ] Duration 2:55–2:58, never above 3:00.
- [ ] MP4 (H.264 + AAC), under 50 MB, plays in a browser and on a phone.
- [ ] Presenter visible bottom-right in every frame, as in the sample; nothing important under the box.
- [ ] Linux visible: the Ubuntu badge, no Windows windows on screen.
- [ ] All three brief scenarios shown and labelled by name; intent with fairness; safety; experiments; CPU.
- [ ] AI compared with the rule baseline, including where rules win.
- [ ] Every emulated element labelled; every number from the section 9 table; no unlicensed media.
- [ ] No personal data on screen (real phone numbers, Wi-Fi names, e-mail, notifications). The lab household
      "Asha Verma" and her number are fictional, as in the repository.
- [ ] File named `LeaderFirstName_TeamName_InstituteName.mp4`; uploaded by the team leader; numbers match the deck.
