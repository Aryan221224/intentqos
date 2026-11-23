"""Plain-language boost requests, turned into checked leases.

"Keep my interview smooth for 45 minutes", "prioritise the TV until 10 pm", "gaming on the console
tonight", "meri call 1 ghante ke liye smooth rakho". The parser here is rule-based and runs on the router
(English and a few Hindi words). It only DRAFTS a lease; the draft then goes through the same path as
any request: schema validation (`validate`), the daily budget, admission control and bargaining
(intents.py), and the shield. Nothing a sentence says can bypass those, and the app shows the
understood request ("Low-latency boost for Work laptop for 45 minutes") for confirmation first.

A language model can be plugged in as an alternative drafter (`draft_with(callable)`): its output must
pass `validate` exactly like the rule parser's, so it can never produce a lease the engine would not
accept from the household directly.
"""
from __future__ import annotations

import re
import time

GOALS = ("low_latency", "throughput")
REALTIME_WORDS = r"call|meeting|interview|class|lecture|zoom|teams|meet|webex|video ?call|voice|game|gaming|match|stream(ing)? live"
THROUGHPUT_WORDS = r"download|upload|backup|update|install|throughput|speed up|faster download"
HINDI = {"ghanta": "hour", "ghante": "hours", "ghanton": "hours", "minat": "minutes", "mint": "minutes",
         "baje": "", "tak": "until", "raat": "tonight", "aaj raat": "tonight", "meri": "my", "mera": "my"}


def _normalise(text: str) -> str:
    t = " " + text.lower().strip() + " "
    for hi, en in HINDI.items():
        t = re.sub(rf"\b{hi}\b", en, t)
    return re.sub(r"\s+", " ", t).strip()


def _duration_s(t: str, now: float) -> tuple[float | None, str]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours)\b", t)
    if m:
        return float(m.group(1)) * 3600, f"{m.group(1)} hour(s)"
    m = re.search(r"(\d+)\s*(m|min|mins|minute|minutes)\b", t)
    if m:
        return float(m.group(1)) * 60, f"{m.group(1)} minutes"
    if re.search(r"\b(an|one|1) hour\b|\bhour\b", t):
        return 3600.0, "1 hour"
    if re.search(r"\bhalf (an )?hour\b", t):
        return 1800.0, "30 minutes"
    m = re.search(r"\b(?:until|till|upto|up to|by)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t) or \
        re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s+until\b", t)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if ap is None and h < 8:
            h += 12                         # "until 5" in a home means 5 pm
        lt = time.localtime(now)
        end = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
        if end <= now:
            end += 86400
        return end - now, f"until {h:02d}:{mi:02d}"
    if "tonight" in t:
        lt = time.localtime(now)
        end = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 23, 0, 0, 0, 0, -1))
        return (end - now if end > now else 3600.0), "until 23:00"
    return None, ""


def parse(text: str, devices: dict, requester_ip: str | None = None, now: float | None = None) -> dict:
    """devices: ip -> name. Returns {"ok", "request"|"reason", "understood"}."""
    now = time.time() if now is None else now
    if not text or len(text) > 300:
        return {"ok": False, "reason": "say what to boost, e.g. 'keep my call smooth for 45 minutes'"}
    t = _normalise(text)
    goal = "throughput" if re.search(THROUGHPUT_WORDS, t) else "low_latency"
    if goal == "low_latency" and not re.search(REALTIME_WORDS + r"|smooth|priorit|boost|lag|fast", t):
        return {"ok": False, "reason": "I could not tell what to boost; try 'boost my video call for an hour'"}
    target, tname = None, None
    for ip, name in sorted(devices.items(), key=lambda kv: -len(kv[1] or "")):
        n = (name or "").lower().strip()
        if n and re.search(rf"\b{re.escape(n)}\b", t):
            target, tname = ip, name
            break
    if target is None:
        for word in ("laptop", "tv", "console", "phone", "desktop", "tablet", "pc"):
            if re.search(rf"\b{word}\b", t):
                hit = [ip for ip, n in devices.items() if word in (n or "").lower()]
                if len(hit) == 1:
                    target, tname = hit[0], devices[hit[0]]
                break
    if target is None and requester_ip and re.search(r"\b(my|this|me)\b", t):
        target, tname = requester_ip, devices.get(requester_ip) or "this device"
    if target is None:
        return {"ok": False, "reason": "which device? name it, e.g. 'on the work laptop'"}
    dur, dtxt = _duration_s(t, now)
    if dur is None:
        dur, dtxt = 3600.0, "1 hour (default)"
    req = {"target": target, "goal": goal, "duration_s": round(min(dur, 8 * 3600)), "priority": 2,
           "label": text.strip()[:80], "requester": "household"}
    err = validate(req)
    if err:
        return {"ok": False, "reason": err}
    what = "Low-latency boost" if goal == "low_latency" else "Throughput boost"
    return {"ok": True, "request": req, "understood": f"{what} for {tname} for {dtxt}"}


def validate(req: dict) -> str | None:
    """The schema every drafted lease must pass (rule parser or language model alike)."""
    if not isinstance(req, dict):
        return "not a request"
    allowed = {"target", "goal", "duration_s", "priority", "label", "requester", "latency_ms",
               "demand_down_kbit", "demand_up_kbit", "start"}
    extra = set(req) - allowed
    if extra:
        return f"unexpected fields: {', '.join(sorted(extra))}"
    if req.get("goal") not in GOALS:
        return "goal must be low_latency or throughput"
    if not isinstance(req.get("target"), str) or not re.fullmatch(r"[0-9a-fA-F:.]{3,45}", req["target"]):
        return "target must be a device address"
    try:
        d = float(req.get("duration_s", 0))
    except (TypeError, ValueError):
        return "duration must be a number"
    if not 60 <= d <= 8 * 3600:
        return "duration must be between 1 minute and 8 hours"
    if int(req.get("priority", 2)) not in (1, 2, 3):
        return "priority must be 1, 2 or 3"
    return None


def draft_with(drafter, text: str, devices: dict, requester_ip: str | None = None) -> dict:
    """Use an external drafter (e.g. a language model) that returns a lease dict; it must pass `validate`."""
    try:
        req = drafter(text, devices, requester_ip)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"drafter failed: {e}"}
    err = validate(req)
    if err:
        return {"ok": False, "reason": f"draft refused: {err}"}
    return {"ok": True, "request": req, "understood": f"{req['goal']} for {devices.get(req['target'], req['target'])}"}
