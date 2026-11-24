"""Emulated speed-test endpoints in the style of speed.cloudflare.com (GET /__down?bytes=N, POST /__up), served
from the emulated internet so the Test Kit's downloads and uploads cross the emulated fibre line.
usage: speedcf.py BIND_IP PORT"""
import http.server, sys
from urllib.parse import urlparse, parse_qs
CHUNK = bytes(65536)
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/__down":
            self.send_error(404); return
        n = int(parse_qs(u.query).get("bytes", ["0"])[0])
        self.send_response(200); self.send_header("Content-Length", str(n)); self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store"); self.end_headers()
        try:
            while n > 0:
                k = min(n, len(CHUNK)); self.wfile.write(CHUNK[:k]); n -= k
        except (BrokenPipeError, ConnectionResetError):
            pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            while True:
                ln = self.rfile.readline()
                if not ln: break
                k = int(ln.split(b";")[0].strip() or b"0", 16)
                if k == 0:
                    self.rfile.readline(); break
                while k > 0:
                    b = self.rfile.read(min(k, 65536))
                    if not b: break
                    k -= len(b)
                self.rfile.readline()
        else:
            while n > 0:
                b = self.rfile.read(min(n, 65536))
                if not b: break
                n -= len(b)
        body = b"{}"
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
http.server.ThreadingHTTPServer.daemon_threads = True
http.server.ThreadingHTTPServer((sys.argv[1], int(sys.argv[2])), H).serve_forever()
