"""Drive IntentQoS Router Setup's window like a person would, screenshotting each step (runs in w-ops)."""
import json, sys, time
from playwright.sync_api import sync_playwright
url, outdir, pw, pin, down, up = sys.argv[1:7]
log = open(f"{outdir}/router-setup-ui.jsonl", "a")
def mark(ev, **k):
    log.write(json.dumps({"t": time.time(), "event": ev, **k}) + "\n"); log.flush(); print(ev, k, flush=True)
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_context(viewport={"width": 900, "height": 760}, device_scale_factor=1.5).new_page()
    page.goto(url, wait_until="networkidle")
    page.wait_for_function("document.getElementById('host') && document.getElementById('host').value.length > 0", timeout=60000)
    time.sleep(1); page.screenshot(path=f"{outdir}/10-router-setup-1-found.png"); mark("found", host=page.input_value("#host"))
    page.click("#next1"); time.sleep(1)
    page.fill("#pw", pw); page.screenshot(path=f"{outdir}/11-router-setup-2-password.png")
    page.click("#next2"); mark("connect_clicked")
    page.wait_for_selector("#p3:not(.hidden)", timeout=120000); time.sleep(1)
    facts = page.inner_text("#facts"); mark("facts", text=facts)
    page.fill("#down", down); page.fill("#up", up)
    if not page.is_checked("#pair"): page.check("#pair")
    page.fill("#pin", pin)
    page.screenshot(path=f"{outdir}/12-router-setup-3-plan-and-pin.png", full_page=True)
    page.click("#install"); mark("install_clicked")
    t0 = time.time(); n = 0
    while time.time() - t0 < 1800:
        time.sleep(15); n += 1
        txt = page.inner_text("#pairres") if page.query_selector("#pairres") else ""
        txt += page.inner_text("#h4") if page.query_selector("#h4") else ""
        page.screenshot(path=f"{outdir}/13-router-setup-4-progress-{n:02d}.png")
        lines = page.inner_text("#log").splitlines()
        mark("progress", last=lines[-1] if lines else "")
        if txt.strip() and ("Paired" in txt or "Install stopped" in txt or "pairing failed" in txt or "has not reported" in txt or "Not paired" in txt):
            break
    page.screenshot(path=f"{outdir}/14-router-setup-5-result.png", full_page=True)
    mark("result", text=page.inner_text("body")[:3000], seconds=round(time.time() - t0, 1))
    b.close()
