#!/usr/bin/env python3
"""Render an SVG/HTML file or URL to PNG with headless Chromium (used for docs and the deck).

    python3 scripts/render.py docs/img/architecture.svg docs/img/architecture.png --width 1200 --height 760
    python3 scripts/render.py http://10.255.0.2:8080/ docs/img/gateway-monitor.png --width 1440 --height 1500 --wait 6
"""
import argparse
import os
import sys

from playwright.sync_api import sync_playwright


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--scale", type=float, default=2.0)
    ap.add_argument("--wait", type=float, default=0.5)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--dark", action="store_true")
    a = ap.parse_args()
    url = a.src if a.src.startswith("http") else "file://" + os.path.abspath(a.src)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": a.width, "height": a.height}, device_scale_factor=a.scale,
                        color_scheme="dark" if a.dark else "light")
        pg.goto(url)
        pg.wait_for_timeout(int(a.wait * 1000))
        pg.screenshot(path=a.out, full_page=a.full)
        b.close()
    print(a.out)


if __name__ == "__main__":
    sys.exit(main())
