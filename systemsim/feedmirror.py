"""Caching mirror of downloads.openwrt.org for the emulated internet.

The emulated routers have no real internet, so their package feed points here (plain HTTP; apk/opkg still
verify every package and index signature with the release keys). On a miss the file is fetched from the real
downloads.openwrt.org through the sandbox's proxy and cached, so the router gets the exact release packages.
Every request is logged (time, client, path, bytes, hit/miss) for the system-test capture.
usage: feedmirror.py BIND_IP PORT CACHE_DIR LOG"""
import http.server, os, sys, time, threading, urllib.request, json
BIND, PORT, CACHE, LOG = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
UP = "https://downloads.openwrt.org"
lock = threading.Lock()
def log(rec):
    with lock, open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_HEAD(self): self.serve(head=True)
    def do_GET(self): self.serve()
    def serve(self, head=False):
        path = self.path.split("?")[0]
        if ".." in path: self.send_error(400); return
        local = os.path.join(CACHE, path.lstrip("/"))
        hit = os.path.isfile(local)
        if not hit:
            try:
                with urllib.request.urlopen(UP + path, timeout=60) as r:
                    data = r.read()
                os.makedirs(os.path.dirname(local), exist_ok=True)
                with open(local + ".part", "wb") as f: f.write(data)
                os.replace(local + ".part", local)
            except Exception as e:
                code = getattr(e, "code", 502)
                log({"t": time.time(), "client": self.client_address[0], "path": path, "status": code, "err": str(e)[:120]})
                self.send_error(code if isinstance(code, int) else 502); return
        size = os.path.getsize(local)
        self.send_response(200); self.send_header("Content-Length", str(size))
        self.send_header("Content-Type", "application/octet-stream"); self.end_headers()
        if not head:
            with open(local, "rb") as f:
                while True:
                    b = f.read(65536)
                    if not b: break
                    self.wfile.write(b)
        log({"t": time.time(), "client": self.client_address[0], "path": path, "status": 200, "bytes": size, "hit": hit})
http.server.ThreadingHTTPServer.allow_reuse_address = True
http.server.ThreadingHTTPServer((BIND, PORT), H).serve_forever()
