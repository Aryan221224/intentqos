# Field-calibrated emulation: an Indian fibre home in the evening

`results/` was measured on a clean lab link (an emulated 100/20 Mbit/s line, every device wired). This page
describes a second, harder test bed that imitates a real Indian home, and what it changed. It is still
**emulation**: no real ISP line, Wi-Fi radio or 4G modem is involved. Every parameter below is taken from
published measurements or marked as an engineering estimate.

Run it with `IQ_PROFILE=india_ftth_evening python3 -m testbed.run_eval --seeds 10 --keep-raw`; results go to
`results-india_ftth_evening/`. The profile (values, reasoning and source links) is
`testbed/profiles/india_ftth_evening.json`.

## What the profile emulates

| Part | Model | Basis |
|---|---|---|
| Plan | 100/100 Mbit/s FTTH, provisioned 2 % above plan | Jio Fiber and BSNL 100 Mbit/s plans are symmetric; ISPs provision slightly above plan |
| ISP queue | byte-limited FIFO, 50 ms at plan rate (625 kB) | US fibre loaded-latency medians and tails; a BNG default of 100 ms bounds it from above |
| Fibre path | 5 ms down / 4 ms up one way, 0.5 ms jitter, GPON upstream grant wait 0.1–2 ms, 0.01 % loss plus Gilbert-Elliott bursts | TRAI-reported latency 5–17 ms and loss 0–0.07 % for Indian fixed broadband; ITU-T G.984 |
| Evening load | 85 % of the time 90–100 % of plan, 12 % at 60–90 %, 3 % at 30–60 %; 30 s mean hold | Fibre delivers 90–100 % of plan in the evening on average (Opensignal India, Ofcom, ACCC, FCC); timing is an estimate |
| Line drop | **failover to a 4G backup**: 20/8 Mbit/s, 300 ms modem buffer, +15 ms, 4 ms jitter, 0.5 % loss | India's median 4G download is about 19 Mbit/s; LTE modems buffer 200–500 ms |
| Home Wi-Fi | per station: lognormal access delay (5 GHz median 1 ms, 2.4 GHz 2 ms), growth above 50 % airtime, stall episodes (Poisson in time, 1 per 30 s on 5 GHz, 1 per 10 s on 2.4 GHz, Pareto length 10–200 ms), short background-scan absences, phone power-save wake-ups; laptop, console, phone and one TV on 5 GHz, two TVs on 2.4 GHz; desktop and NAS wired | Sui et al., MobiSys 2016 (a busy campus WLAN: median 3 ms, p90 about 20 ms, p99 above 250 ms; a home is quieter); stall rates are an estimate |
| Servers | BBR (as Google and Akamai serve downloads); clients CUBIC | published CDN deployments |
| Router CPU | the engine gets 20 % of one vCPU (cgroup quota), roughly a Raspberry Pi 4 core | PassMark and Geekbench single-thread ratios |

Wi-Fi is applied on the WAN side of the router (the emulator sits in the ISP namespace), so it adds delay
outside the router's control, as real Wi-Fi does, but the router's shaper sees the packets before their
Wi-Fi delay on upload. Scan and stall events are scheduled in time from a per-station random stream seeded
per run, so every arm of an experiment sees the same events at the same moments (common random numbers).

## What it found

The realistic test bed exposed faults that the clean lab never triggered. We fixed each one, tested the fix,
and re-ran every arm. They are listed because they are the most useful result of this exercise. Faults 5-8
were found by an independent review of the first full run, which showed that 2 of 10 IntentQoS runs (and 4
of 10 rules-only runs) still collapsed after the failover and that the harness had silently dropped them.

| # | Fault | Symptom | Fix |
|---|---|---|---|
| 1 | **A longer path looked like a queue.** After a failover the path is 15 ms longer. The baseline only rises while the line is quiet, so under load the estimator read the extra delay as bufferbloat and cut to its floor. | download held at 10 Mbit/s on a 20 Mbit/s 4G link for the rest of the run | drain probe (like BBR's ProbeRTT): after 3 cuts with a flat delay, hold the shaper at its floor for 0.8 s; if the delay is still high, re-learn the baseline (`estimator.py`) |
| 2 | **The floor was above the backup link.** The minimum rate was 10 % of the plan: 10 Mbit/s on a 100/100 plan, above the 8 Mbit/s 4G uplink. | upload queue of 50-100 ms the engine could not remove | floor = max(2 % of plan, 1 Mbit/s) |
| 3 | **Unsure meant "use the rules", even for an established flow.** A TV whose buffer drains fetches segments back to back; the model became unsure (still preferring "streaming") and the rules called it bulk. | TV rebuffering 21 s vs 7 s with no QoS after a failover | a flow keeps the class it earned from a confident decision while that class is still in the model's prediction set (`ml-hold`) |
| 4 | **After a re-learn, the rate drifted back to the plan rate.** | overshoot of the 4G link, 60-90 ms spikes | drift toward the plan rate only 30 s after the last queue; a queue deeper than 100 ms is cut to what the line delivered |
| 5 | **The drain probe could not run at the floor,** and a path change sitting right at the 15 ms threshold flickered between "queue" and "healthy", which reset the count of flat cuts. | rate stuck at 2 Mbit/s for the rest of the run | the probe may run at the floor; the flat-cut count survives healthy flickers (cuts up to 15 s apart) |
| 6 | **A filling queue was taken for a longer path.** A probe started the instant the line switched read the 4G modem's 300 ms buffer, not yet drained, as a 252 ms longer path. | engine blind to a real queue for several seconds | a growing delay is not "flat"; a "longer path" above 100 ms is treated as a queue still draining |
| 7 | **A 4G path change sits right at the bloat threshold.** 4G adds about 15 ms one way, the same as the 15 ms bloat threshold, so the drain probe's verdict was a coin toss; and a probe started while the 4G modem's buffer was still full was inconclusive yet blocked the next probe for 20 s. | download stuck at the floor in some runs | after the drain hold, any lasting residual above 5 ms is the path; an inconclusive probe may repeat after 4 s |
| 8 | **The router's own delay probes queued behind its upload.** At a low shaped rate even the top class waits behind the packet being sent, so the estimator measured its own queue and kept cutting. | upload stuck at the 2 Mbit/s floor | probes go to their own HTB class outside the shaped tree (`enforce.py`), so they measure the line, not us |

The test harness and emulator had faults too, found the same way and fixed before the runs reported here:

* the Wi-Fi model let one station's stall hold back every other station's uploads (and the router's probes);
* per-packet random draws meant different arms saw different Wi-Fi spikes (now common random numbers);
* a starved download or TV client kept waiting for queued data past the run window (a trickle of data meant its
  read never timed out) and was stopped before it wrote its result, so the worst runs silently dropped out of
  the medians. Every client now stops at its own
  deadline and always writes what it measured; a TV segment still downloading at the end counts as stall time,
  and a page still loading counts with its elapsed time.

## Results

See `results-india_ftth_evening/REPORT.md` (all metrics, medians, IQR, bootstrap CIs, Mann-Whitney tests) and
`results-india_ftth_evening/CLASSIFIER_FIELD.md` (classifier accuracy on this traffic). A summary is added
below once the runs complete.
