"""The 4G bridge laptop: open IntentQoS 4G Bridge, check it found the phone and the Ethernet port, press Share,
then keep the window open until STOPFILE exists (closing the window would quit the program), then Stop sharing."""
import json, os, sys, time
from playwright.sync_api import sync_playwright
url, outdir, stopfile = sys.argv[1:4]
log = open(f"{outdir}/bridge-ui.jsonl", "a")
def mark(ev, **k):
    log.write(json.dumps({"t": time.time(), "event": ev, **k}) + "\n"); log.flush(); print(ev, k, flush=True)
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_context(viewport={"width": 820, "height": 700}, device_scale_factor=1.5).new_page()
    page.goto(url, wait_until="networkidle"); time.sleep(4)
    page.screenshot(path=f"{outdir}/50-bridge-found.png", full_page=True)
    mark("found", state=page.inner_text("#state")[:800], phone=page.input_value("#phone"), eth=page.input_value("#eth"))
    page.click("#share"); time.sleep(5)
    page.screenshot(path=f"{outdir}/51-bridge-sharing.png", full_page=True)
    mark("shared", msg=page.inner_text("#msg"), state=page.inner_text("#state")[:800])
    while not os.path.exists(stopfile):
        time.sleep(2)
    page.screenshot(path=f"{outdir}/52-bridge-before-stop.png", full_page=True)
    page.click("#unshare"); time.sleep(4)
    page.screenshot(path=f"{outdir}/53-bridge-stopped.png", full_page=True)
    mark("unshared", msg=page.inner_text("#msg"))
    b.close()
