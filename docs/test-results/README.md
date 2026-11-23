# Test and evaluation results (2.0.0)

Captured 2026-11-23 on a Linux 6.18 sandbox (emulation, no router or Windows hardware).

| File | What it is |
|---|---|
| `python-tests.txt` | `python -m unittest discover -s tests -v`: 110 tests, all OK |
| `go-testkit-tests.txt` | `go test -tags testkit ./...` in `kit/`: test-kit and lab endpoint tests, PASS |
| `ebpf_compare.json` | eBPF vs userspace capture comparison from the netns emulation |
| `../drift_eval.json` | Model drift evaluation |
| `../../results-ablation/` | Outcome control on vs off (4 seeds per arm, per-run metrics included); no significant effect |
| `../../results/`, `../../results-india_ftth_evening/` | Scenario reports and figures |

Windows and ARM/MIPS helpers were compiled but not run on real hardware.
