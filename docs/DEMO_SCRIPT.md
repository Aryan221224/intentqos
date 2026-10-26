# Live demo runbook (about 6 minutes)

One rehearsed path through all three parts: the gateway, the operator console and the subscriber
app. Runs on one Windows laptop with WSL 2, or on one Linux machine.

## Before you present (10 minutes earlier)

- **Windows:** double-click `dist/IntentQoS Demo (all three).exe`. Three windows open: operator
  console (wide), subscriber app (phone-sized) and gateway console. Arrange them: gateway left,
  operator right, app in front.
- **Linux:** `sudo scripts/stack.sh up`, then open http://localhost:8090/, http://localhost:8088/ops/
  and http://localhost:8088/app/ (the app in a narrow window).
- The lab starts a video call and browsing by itself (`labd --presets call,browse`); if you restarted it,
  turn them on in **Experiments → Inject conditions by hand**. Check that **System → Setup check** is green
  and the grade is A or A+.
- Sign in: operator console *Admin*; app *Open Asha Verma's home (lab)*.

Backup if the laptop misbehaves: `docs/console-replay.html` (the gateway console replaying a recorded
session, labelled as a replay) and the screenshots in `docs/img/`.

## The story

| Time | Window | You do | You say | They see |
|---|---|---|---|---|
| 0:00 | Gateway, Monitor | point at the mixing desk | "This runs on the home router. Each strip is one class of traffic: calls, video, streaming, downloads. The white mark is what each class is guaranteed." | Voice and video strips active, delay near zero, grade A+ |
| 0:20 | Gateway, Experiments | under *Inject conditions by hand*, turn on *ISO download*, *Cloud backup*, *Three TVs* | "A big download, a backup and three TVs start. On a normal router the call would now freeze." | Bulk and best-effort strips fill; the delay chart stays flat; grade stays A |
| 0:45 | Gateway, Traffic | point at the TV rows | "It recognises adaptive video from packet sizes and timing alone, with a confidence, and serves it above downloads. One click corrects a label." | TVs labelled *Streaming*, confidence near 100 %, lane *Best effort* |
| 1:05 | Gateway, Link | *Drop to 20/5* | "The ISP line silently drops to 20 Mbit/s. Nobody tells the router." | Delay spike, then the shaper steps down within a second and the delay is flat again; **Policy** says why |
| 1:35 | App, Home | show the line card | "This is what the family sees on their phone, from the ISP, in English or Hindi." | "Smooth for everyone", the line filled by each device, plan and renewal |
| 1:50 | App, Boost | *Asha's laptop*, *Calls and games*, *1 hour* | "Mum has a board meeting. Before she commits she sees what is reserved and who shares the rest. It ends by itself." | Preview, then the boost on Home with a countdown and *Working*; the gateway's **Prioritise** shows the same lease |
| 2:20 | App, language | tap *हिन्दी* | "Indian ISPs ship bilingual apps; so does this one." | The app in Hindi |
| 2:35 | Operator, Fleet overview | point at the access-network strip | "This is the ISP's side: 241 homes, one square each, grouped by the OLT they hang off." | Mostly green squares; the real gateway outlined |
| 2:50 | Operator | *Simulate a network problem*: GN-OLT-01, congestion | "Now a fibre node gets congested. Watch: the controller has to notice by itself." | Within about two minutes the GN-OLT-01 row turns orange, **one** incident opens ("23 of 24 homes affected") instead of 23 alerts |
| 3:40 | App, Home | refresh | "Asha's home is on that node. Her app already says so, and that she doesn't need to restart anything." | "Network issue in your area" notice |
| 3:55 | Operator, a home | open SN-100001 (Asha) | "A support agent opening her home sees the verdict first: is it the home or the network?" | Diagnosis, devices, chart, events; *Run line test*; *Open live console* opens the gateway console through the router's agent, audited |
| 4:40 | Operator, Rollouts | point at the earlier rollouts | "Every model and config change is signed and rolled out in stages. This one made homes worse: the guard stopped it after 33 seconds and rolled 20 homes back." | Rolled-back and completed rollouts with the statistics |
| 5:10 | Gateway, Experiments | open S2 and S3 | "The same stories measured over 10 seeded runs per arm against no QoS, static QoS and our own rule baseline, including where we lose." | Medians, spread, p-values, figures |
| 5:40 | Gateway, Experiments | *Crash the engine* (optional) | "And if the engine crashes, the watchdog restores a safe state in under two seconds." | Red banner; traffic keeps flowing; restart |

## If something goes wrong

- A window says it did not start: close it and run the launcher again; the log path is on screen.
  In WSL: `sudo bash /opt/intentqos/scripts/stack.sh status`.
- The gateway console says "Engine not reachable": **Experiments → Restart engine**.
- No traffic: turn the presets on again in **Experiments → Inject conditions by hand**.
- The incident does not open: give it two minutes (four reports of 15 s, then a correlation pass);
  pick an OLT with more homes (GN-OLT-07 has 31).
- Fall back to `docs/console-replay.html` and the screenshots.

## Questions you will likely get

- **"Is this real hardware?"** The gateway engine is real and shapes real packets, but the access
  link, the household devices and the modem buffer are emulated in Linux network namespaces, and 240 of
  the 241 homes in the fleet are simulated (labelled as such). The router package is a real OpenWrt
  `.ipk`; it has run on x86 and, under emulation, on aarch64.
- **"Where exactly is the AI, and does it help?"** Only in classification. It abstains when unsure
  (conformal prediction) and the scheduling stays deterministic and shielded. It helps where the class
  matters: TV rebuffering 3 s instead of 18 s after a link drop; web pages 76 instead of 142 ms during a
  download. It costs the update download in S3 (25 vs 54 Mbit/s by default); the bulk-friendly profile
  measured 38 Mbit/s with TVs still better than with rules.
- **"Do you read my traffic?"** No payload bytes are parsed (a unit test proves it), and no domain
  names. Only sizes, timing, direction and protocol number. The subscriber controls what is shared
  with the ISP and can see every access.
- **"What stops one person from prioritising forever?"** Leases expire (at most 8 h), each requester
  has a daily budget, all leases together are capped at 60 % of the line, and every class keeps a floor.
- **"How would an ISP roll this out?"** OpenWrt/prplOS package in the router image, outbound-only agent
  enrolled with a claim code, signed configs and models in stages with an automatic guard; the protocol
  maps onto TR-369/USP (`docs/AGENT_PROTOCOL.md`).
