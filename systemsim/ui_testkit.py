"""Drive the IntentQoS Test Kit window on the work laptop like a person: pair, choose tests, run, watch, finish.
    ui_testkit.py URL OUTDIR run   PIN CONSOLE SECONDS SCEN1,SCEN2,...
    ui_testkit.py URL OUTDIR failover QOS(on|off) MOVE_CMD BACK_CMD
In failover mode, when the window asks for the cable to be moved, MOVE_CMD is run (the "person" moves the
router's WAN cable to the 4G bridge laptop), then "Cable moved" is pressed; BACK_CMD runs at the end."""
import json, subprocess, sys, time
from playwright.sync_api import sync_playwright
url, outdir, mode = sys.argv[1:4]
log = open(f"{outdir}/testkit-ui.jsonl", "a")
def mark(ev, **k):
    log.write(json.dumps({"t": time.time(), "event": ev, **k}) + "\n"); log.flush(); print(ev, json.dumps(k)[:300], flush=True)
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_context(viewport={"width": 1000, "height": 900}, device_scale_factor=1.5).new_page()
    page.goto(url, wait_until="networkidle"); time.sleep(2)
    tag = "30" if mode == "run" else "40"
    if mode == "run":
        pin, console, secs, scen = sys.argv[4:8]
        page.screenshot(path=f"{outdir}/{tag}-testkit-start.png", full_page=True)
        page.click("text=Or pair with the Operator Console"); time.sleep(0.5)
        if not page.input_value("#console"):
            page.fill("#console", console)
        page.fill("#pin", pin)
        page.screenshot(path=f"{outdir}/{tag}-testkit-pairing.png", full_page=True)
        page.click("#pairbtn")
        page.wait_for_function("document.getElementById('checkres').innerText.includes('connected')", timeout=60000)
        mark("paired", check=page.inner_text("#checkres"), pair=page.inner_text("#pairres"))
        want = scen.split(",")
        for cb in page.query_selector_all("#scen input[type=checkbox]"):
            v = cb.get_attribute("value")
            (cb.check if v in want else cb.uncheck)()
        page.select_option("#secs", secs)
        page.screenshot(path=f"{outdir}/{tag}-testkit-ready.png", full_page=True)
        page.click("#run"); mark("run_clicked", scenarios=want, seconds=secs)
    else:
        qos, move, back = sys.argv[4:7]
        page.wait_for_function("document.getElementById('checkres').innerText.includes('connected')", timeout=60000)
        page.click("#fo"); time.sleep(0.5)
        page.screenshot(path=f"{outdir}/{tag}-failover-start-{qos}.png", full_page=True)
        page.click("#foon" if qos == "on" else "#fooff"); mark("failover_clicked", qos=qos)
    t0 = time.time(); n = 0; moved = False
    while time.time() - t0 < 3600:
        time.sleep(1 if mode != "run" else 3)
        live = page.evaluate("fetch('/api/live').then(r => r.json())")
        if mode != "run" and live.get("waiting") and not moved:
            mark("asked_to_move_cable", text=live.get("waiting"))
            page.screenshot(path=f"{outdir}/{tag}-failover-move-cable-{qos}.png", full_page=True)
            r = subprocess.run(move, shell=True, capture_output=True, text=True)
            mark("cable_moved", rc=r.returncode, out=(r.stdout + r.stderr)[-300:])
            page.click("#moved"); moved = True
        if int(time.time() - t0) // 20 > n:
            n += 1
            page.screenshot(path=f"{outdir}/{tag}-testkit-{mode}-{qos if mode != 'run' else 'live'}-{n:02d}.png", full_page=True)
            mark("live", phase=live.get("phase"), progress=live.get("progress"), down=live.get("down_mbps"), up=live.get("up_mbps"))
        if not live.get("running") and live.get("done"):
            break
    time.sleep(2)
    page.screenshot(path=f"{outdir}/{tag}-testkit-{mode}-result{'-' + qos if mode != 'run' else ''}.png", full_page=True)
    mark("result", text=page.inner_text("#resbox")[:6000], seconds=round(time.time() - t0, 1))
    if page.is_visible("#finish"):
        page.click("#finish"); time.sleep(3)
        mark("finished", upl=page.inner_text("#upl") if page.query_selector("#upl") else "")
    if mode != "run":
        r = subprocess.run(back, shell=True, capture_output=True, text=True); mark("cable_back", rc=r.returncode)
    b.close()
