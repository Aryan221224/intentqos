# IntentQoS -- one-command workflows. Targets that touch the network need root (sudo make ...).
PY ?= python3
MUSL_CROSS ?= /opt/musl-cross
SEEDS ?= 10
CONFIG ?= config/testbed.toml

.PHONY: help lab router ipk controller stack stack-down testbed clean-testbed engine watchdog demo dataset train eval report test render replay all

help:
	@echo "make stack | stack-down | controller | router | ipk | lab | testbed | engine | watchdog | demo | dataset | train | eval SEEDS=10 | report | replay | test | clean-testbed"

router:             ## cross-compile the capture helper (needs musl cross toolchains in MUSL_CROSS=…/bin dirs) + host build
	cc -O2 -Wall -Wextra -o router/iq-capture router/iq-capture.c
	@if command -v clang >/dev/null; then \
	  inc=$$(ls -d /usr/include/$$(uname -m)-linux-gnu 2>/dev/null | head -1); \
	  clang -O2 -g -Wall -target bpfel $${inc:+-I$$inc} -c router/iq-flow.bpf.c -o router/iq-flow.bpf.o && \
	  clang -O2 -g -Wall -target bpfeb $${inc:+-I$$inc} -c router/iq-flow.bpf.c -o router/iq-flow-eb.bpf.o && \
	  { s=$$(command -v llvm-strip || true); [ -n "$$s" ] && $$s -g router/iq-flow.bpf.o router/iq-flow-eb.bpf.o; true; } && \
	  echo "built kernel flow engine (bpfel, bpfeb)"; else echo "skip kernel flow engine (no clang)"; fi
	@for t in aarch64-unknown-linux-musl:aarch64:aarch64-linux-musl: armv7-unknown-linux-musleabihf:armv7:arm-linux-musleabihf:-mcpu=cortex_a7 \
	         mipsel-unknown-linux-musl:mipsel:mipsel-linux-musleabi:-mcpu=mips32r2+-msoft-float x86_64-unknown-linux-musl:x86_64:x86_64-linux-musl:; do \
	  tc=$$(echo $$t | cut -d: -f1); n=$$(echo $$t | cut -d: -f2); zt=$$(echo $$t | cut -d: -f3); zf=$$(echo $$t | cut -d: -f4 | tr + ' '); \
	  cc=$(MUSL_CROSS)/$$tc/bin/$$tc-gcc; \
	  if [ -x "$$cc" ]; then $$cc -O2 -static -s -Wall -o router/bin/iq-capture.$$n router/iq-capture.c && echo "built $$n"; \
	  elif $(PY) -m ziglang version >/dev/null 2>&1; then $(PY) -m ziglang cc -target $$zt $$zf -O2 -static -s -Wall -o router/bin/iq-capture.$$n router/iq-capture.c && echo "built $$n (zig cc)"; \
	  else echo "skip $$n (no $$cc; pip install ziglang to cross-compile with zig)"; fi; done

ipk:                ## OpenWrt packages in router/dist/
	$(PY) router/build_ipk.py

controller:         ## operator controller on https://localhost:8443/ (ops console /ops/, consumer app /app/)
	@[ -f controller/data/pki/server.pem ] || $(PY) -m controller.pki --dir controller/data/pki --ip 10.255.0.1 --ip 127.0.0.1 --dns localhost
	@[ -f controller/data/controller.db ] || $(PY) -m controller.seed --sim 240
	$(PY) -m controller.server --config controller/controller.toml

stack:              ## controller + simulated fleet + testbed + engine + agent, in the background
	scripts/stack.sh up

stack-down:
	scripts/stack.sh down

lab:                ## testbed + engine + watchdog + console with lab controls on http://localhost:8090/
	$(PY) -m testbed.labd

testbed:            ## create the 4-namespace emulated home network (100/20 Mbit/s, bloated modem)
	$(PY) testbed/testbed.py up --down 100 --up 20

clean-testbed:
	$(PY) testbed/testbed.py down

engine:             ## run IntentQoS in the gateway namespace; dashboard http://10.255.0.2:8080/
	ip netns exec iq-gw $(PY) -m intentqos.daemon --config $(CONFIG)

watchdog:           ## run the dead-man watchdog in the gateway namespace
	ip netns exec iq-gw $(PY) -m intentqos.watchdog --config $(CONFIG)

demo:               ## scripted 80 s golden-path demo (engine must be running)
	$(PY) scripts/demo.py

dataset:            ## generate the labelled header-only flow dataset (~25 min)
	$(PY) ml/gen_dataset.py --sessions 24 --seconds 50 --out ml/data/dataset.jsonl

train:              ## train + calibrate + conformal threshold; writes models/ and docs/CLASSIFIER_REPORT.md
	$(PY) ml/train.py --data ml/data/dataset.jsonl --out models/flowclf.joblib

eval:               ## all scenarios x arms x seeds (resumable)
	$(PY) -m testbed.run_eval --seeds $(SEEDS) --resume

report:             ## statistics, figures, results/REPORT.md and results/report.html
	$(PY) -m testbed.report

test:               ## unit tests (no root needed)
	$(PY) -m unittest discover -s tests -v

replay:             ## record a 150 s session from the running lab and build docs/console-replay.html
	$(PY) scripts/record_replay.py --out results/replay.json
	$(PY) scripts/build_replay.py results/replay.json docs/console-replay.html

render:             ## re-render the architecture diagram PNG
	$(PY) scripts/render.py docs/img/architecture.svg docs/img/architecture.png --width 1200 --height 760 --scale 1.5

all: testbed dataset train eval report
