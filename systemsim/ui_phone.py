"""The household phone (Pixel 7 profile, on the router's 5 GHz Wi-Fi) opens the subscriber app and signs in."""
import json, re, sys, time
from playwright.sync_api import sync_playwright
url, outdir, mobile = sys.argv[1:4]
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(**p.devices["Pixel 7"], locale="en-IN", timezone_id="Asia/Kolkata")
    page = ctx.new_page()
    page.goto(url, wait_until="networkidle"); time.sleep(2)
    page.screenshot(path=f"{outdir}/20-phone-app-sign-in-hindi.png")
    eng = page.query_selector("text=English")
    if eng:
        eng.click(); time.sleep(1)
    page.screenshot(path=f"{outdir}/20-phone-app-sign-in.png")
    page.fill("#lp", mobile); page.click("#lf button[type=submit]")
    try:
        page.wait_for_selector("#otp:not([hidden])", timeout=30000); time.sleep(1)
    except Exception:
        page.screenshot(path=f"{outdir}/21-phone-app-error.png"); print("ERROR:", page.inner_text("#lm")); raise SystemExit(1)
    demo = page.inner_text("#demo"); code = re.search(r"\b(\d{6})\b", demo).group(1)
    page.screenshot(path=f"{outdir}/21-phone-app-code.png")
    page.fill("#lc", code)
    page.click("#lf button[type=submit]")
    time.sleep(6)
    page.screenshot(path=f"{outdir}/22-phone-app-home.png", full_page=True)
    print(page.inner_text("body")[:2500])
    ctx.storage_state(path=f"{outdir}/phone-state.json")
    b.close()
