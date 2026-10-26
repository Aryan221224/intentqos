"""Line-test server for the testbed: lives in the emulated internet (server namespace) so a test from
the router crosses the emulated ISP access link.

    GET /down?seconds=8     stream bytes for N seconds (download capacity)
    POST /up                read and discard (upload capacity)
"""
from __future__ import annotations

import argparse
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CHUNK = bytes(65536)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/down":
            self.send_error(404)
            return
        secs = min(30.0, float(parse_qs(u.query).get("seconds", ["8"])[0]))
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        end = time.monotonic() + secs
        try:
            while time.monotonic() < end:
                self.wfile.write(CHUNK)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        got = 0
        while got < n:
            b = self.rfile.read(min(65536, n - got))
            if not b:
                break
            got += len(b)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(str(got).encode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="0.0.0.0:8099")
    a = ap.parse_args()
    host, port = a.listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), H)
    srv.daemon_threads = True
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
