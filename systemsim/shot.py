"""Open a page inside a laptop's namespace, optionally act on it, screenshot it and print its text.
usage: ip netns exec NS python3 shot.py URL OUT.png [--mobile] [--click SEL]... [--fill SEL=VAL]... [--wait-text T] [--wait S]"""
import argparse, sys, time
from playwright.sync_api import sync_playwright
ap = argparse.ArgumentParser(); ap.add_argument("url"); ap.add_argument("out")
ap.add_argument("--mobile", action="store_true"); ap.add_argument("--step", action="append", default=[])
ap.add_argument("--wait", type=float, default=1.5); ap.add_argument("--full", action="store_true")
ap.add_argument("--width", type=int, default=1280); ap.add_argument("--height", type=int, default=860)
ap.add_argument("--text", action="store_true")
a = ap.parse_args()
with sync_playwright() as p:
    b = p.chromium.launch(args=["--ignore-certificate-errors"])
    ctx = p.devices["Pixel 7"] if a.mobile else {"viewport": {"width": a.width, "height": a.height}, "device_scale_factor": 1.5}
    page = b.new_context(ignore_https_errors=True, **ctx).new_page()
    page.goto(a.url, wait_until="networkidle", timeout=60000)
    time.sleep(a.wait)
    for i, s in enumerate(a.step):
        kind, _, arg = s.partition(":")
        if kind == "click": page.click(arg, timeout=60000)
        elif kind == "fill": sel, _, val = arg.partition("="); page.fill(sel, val)
        elif kind == "check": page.check(arg)
        elif kind == "uncheck": page.uncheck(arg)
        elif kind == "waittext": page.wait_for_selector(f"text={arg}", timeout=900000)
        elif kind == "sleep": time.sleep(float(arg))
        elif kind == "shot": page.screenshot(path=arg, full_page=a.full)
        elif kind == "goto": page.goto(arg, wait_until="networkidle")
    page.screenshot(path=a.out, full_page=a.full)
    if a.text: print(page.inner_text("body")[:4000])
    b.close()
