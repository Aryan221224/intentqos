"""Serial console driver for the router VMs: con.py SOCK [--wait REGEX] [--send TEXT] [--timeout S] [--log FILE]"""
import argparse, re, socket, sys, time
ap = argparse.ArgumentParser()
ap.add_argument("sock"); ap.add_argument("--send", action="append", default=[])
ap.add_argument("--wait", default=None); ap.add_argument("--timeout", type=float, default=60)
ap.add_argument("--log", default=None); ap.add_argument("--quiet", action="store_true")
a = ap.parse_args()
s = socket.socket(socket.AF_UNIX); s.connect(a.sock); s.settimeout(0.5)
buf = b""; log = open(a.log, "ab") if a.log else None
def pump(until=None, timeout=a.timeout):
    global buf
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            d = s.recv(65536)
        except socket.timeout:
            d = b""
        if d:
            buf += d
            if log: log.write(d); log.flush()
            if not a.quiet: sys.stdout.write(d.decode("utf-8", "replace")); sys.stdout.flush()
        if until and re.search(until.encode(), buf):
            return True
    return until is None
for t in a.send:
    s.sendall(t.encode().decode("unicode_escape").encode()); time.sleep(0.3)
ok = pump(a.wait)
sys.exit(0 if ok else 3)
