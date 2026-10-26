# Third-party components and licences

IntentQoS itself is MIT-licensed. The only third-party material it redistributes is three typefaces
under the SIL Open Font License, subset to the characters the consoles use (licence texts next to the
files). Everything else below is a dependency installed by the user, a system tool invoked as a
separate program, a build-time tool, or prior work whose *documented ideas* we re-implemented and credit.

| Component | Licence (SPDX) | How it is used |
|---|---|---|
| Python 3.11 | PSF-2.0 | runtime |
| numpy | BSD-3-Clause | dependency (pip), training only |
| scikit-learn | BSD-3-Clause | dependency (pip), training only: random forest, isotonic calibration (routers run the exported model in pure Python) |
| scipy | BSD-3-Clause | dependency (pip): Mann-Whitney U test in the report |
| matplotlib | Matplotlib License (PSF-based, BSD-compatible) | dependency (pip): report figures |
| joblib | BSD-3-Clause | dependency (pip): model bundle |
| Playwright (optional) | Apache-2.0 | rendering docs images only |
| iproute2 (`ip`, `tc`) | GPL-2.0-only | system tool, invoked as a separate process; not linked or redistributed |
| nftables (`nft`) | GPL-2.0-only | system tool, invoked as a separate process |
| Linux kernel qdiscs (HTB, CAKE, fq_codel, tbf, netem) | GPL-2.0-only | used through the kernel's own interfaces |
| Barlow, Barlow Semi Condensed (Jeremy Tribby) | OFL-1.1 | **bundled, subset**: gateway console (`intentqos/web/fonts/`, licence `OFL-Barlow.txt`) |
| IBM Plex Sans, IBM Plex Mono (IBM) | OFL-1.1 | **bundled, subset**: operator console (`controller/web/fonts/`, licence `OFL-IBMPlex.txt`) |
| Mukta (Ek Type) | OFL-1.1 | **bundled, subset, Latin + Devanagari**: subscriber app (`controller/web/fonts/`, licence `OFL-Mukta.txt`) |
| Go toolchain | BSD-3-Clause | build time only: the Windows launchers; the exes contain the Go runtime (BSD-3-Clause) |
| rsrc (akavel) | MIT | build time only: icons in the Windows launchers; not redistributed |
| fontTools | MIT | build time only: subsetting the fonts |

## Prior work credited (ideas re-implemented, no code copied)

| Work | Licence of the original | What we took |
|---|---|---|
| cake-autorate (lynxthecat) | GPL-2.0 | the published design of a latency-driven, load-gated shaper-rate controller with ICMP-timestamp one-way delay. Our `estimator.py` is an independent implementation from the README/design description. |
| qosify (OpenWrt) | GPL-2.0 | the documented default thresholds (avg packet length 500 B, bulk at 100 pps for 5 s) as the *rule baseline*. No source used. |
| Alshiekh et al., "Safe Reinforcement Learning via Shielding", AAAI 2018 | paper | the shield concept applied to the policy engine |
| Ferguson et al., "Participatory Networking (PANE)", SIGCOMM 2013 | paper | time-bounded, budgeted resource requests ("leases") |
| Cinmere et al., "Direct-Conflict Resolution in Intent-Driven Autonomous Networks", EW 2023 | paper | Nash-bargaining resolution of competing intents |
| Jain, Chiu & Hawe, DEC-TR-301, 1984; Ware et al., HotNets 2019 | papers | fairness index and its demand-aware use |
| ITU-T G.107 E-model | ITU recommendation | simplified MOS estimate in the harness |
| Vovk et al., split conformal prediction | papers | abstaining classifier with coverage guarantee |

Datasets: none downloaded. The training set is generated locally by `ml/gen_dataset.py`
from synthetic traffic; it contains no personal data and no payload bytes.
