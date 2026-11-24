"""The engine must import only what OpenWrt's packages provide.

Found by the full-system emulation on the real OpenWrt 25.12.6 image: `import uuid` (python3-uuid, a separate
package) crash-looped the engine on a router with the packages Router Setup installs. This test keeps the
engine's standard-library imports inside python3-light plus the packages install.sh adds."""
import ast
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "intentqos")
# python3-light + python3-codecs/-email/-urllib/-openssl/-logging (router/kit/install.sh NEED) on OpenWrt 24.10/25.12
AVAILABLE = {"__future__", "argparse", "array", "base64", "binascii", "bisect", "calendar", "collections", "contextlib",
             "copy", "dataclasses", "datetime", "enum", "errno", "fnmatch", "functools", "gzip", "hashlib", "heapq",
             "hmac", "http", "io", "ipaddress", "itertools", "json", "logging", "math", "operator", "os", "platform",
             "random", "re", "secrets", "select", "shlex", "shutil", "signal", "socket", "socketserver", "ssl", "stat",
             "string", "struct", "subprocess", "sys", "tempfile", "textwrap", "threading", "time", "tomllib",
             "traceback", "types", "typing", "urllib", "warnings", "weakref", "zlib", "email", "encodings", "codecs"}


class RouterStdlib(unittest.TestCase):
    def test_engine_imports_are_on_the_router(self):
        bad = {}
        for root, _, files in os.walk(ROOT):
            for f in files:
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(root, f), encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
                for n in ast.walk(tree):
                    names = []
                    if isinstance(n, ast.Import):
                        names = [a.name.split(".")[0] for a in n.names]
                    elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                        names = [n.module.split(".")[0]]
                    for m in names:
                        if m in sys.stdlib_module_names and m not in AVAILABLE:
                            bad.setdefault(m, []).append(f)
        self.assertEqual(bad, {}, "standard-library modules the router's Python does not have")


if __name__ == "__main__":
    unittest.main()


class NetlinkAttrs(unittest.TestCase):
    def test_nested_attributes_and_ints(self):
        import struct
        from intentqos.enforce import _nl_attrs, _nl_int

        def attr(t, payload):
            ln = 4 + len(payload)
            return struct.pack("HH", ln, t) + payload + b"\0" * (((ln + 3) & ~3) - ln)
        tin = attr(2, struct.pack("I", 7)) + attr(3, struct.pack("Q", 1234567890123)) + attr(19, struct.pack("I", 35))
        app = attr(10, attr(1, tin) + attr(2, tin))
        a = _nl_attrs(app)
        tins = _nl_attrs(a[10])
        self.assertEqual(sorted(tins), [1, 2])
        t = _nl_attrs(tins[1])
        self.assertEqual((_nl_int(t[2]), _nl_int(t[3]), _nl_int(t[19])), (7, 1234567890123, 35))
