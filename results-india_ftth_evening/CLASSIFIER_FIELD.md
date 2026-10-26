# Classifier accuracy on this experiment's traffic

Results: `results-india_ftth_evening`. Truth = the flow kind the generator produced; prediction = the engine's committed label each policy tick (the one that drives the shaper). Every flow-tick counts once. EMULATION.

| arm | runs | flow-ticks | scheduling-class accuracy | label accuracy | model confident | model unsure, kept its class | rules |
|---|---|---|---|---|---|---|---|
| adaptive_ml | 50 | 39254 | 0.981 | 0.979 | 0.971 | 0.027 | 0.002 |
| adaptive_ml_bulk | 10 | 10589 | 0.992 | 0.981 | 0.975 | 0.020 | 0.004 |
| adaptive_ml_intent | 10 | 9405 | 0.935 | 0.935 | 0.994 | 0.002 | 0.004 |
| adaptive_rules | 30 | 28367 | 0.669 | 0.571 | 0.000 | 0.000 | 1.000 |

Per true class (scheduling-class accuracy):

| arm | voip | video | gaming | streaming | web | bulk |
|---|---|---|---|---|---|---|
| adaptive_ml | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.969 |
| adaptive_ml_bulk |  |  | 1.000 | 1.000 |  | 0.986 |
| adaptive_ml_intent | 1.000 | 1.000 |  |  |  | 0.889 |
| adaptive_rules | 1.000 | 1.000 | 1.000 | 0.332 | 1.000 | 0.640 |

Matched comparison (same scenarios, s1_iso_during_call, s2_wan_drop, s3_tvs_and_gaming; same seeds):

| arm | flow-ticks | scheduling-class accuracy |
|---|---|---|
| adaptive_ml | 28385 | 0.993 |
| adaptive_rules | 28367 | 0.669 |
