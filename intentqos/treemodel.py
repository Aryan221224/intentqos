"""Pure-Python runtime for the flow classifier, for routers.

The trained model (random forest + isotonic calibration, from ml/train.py) is exported by
ml/export_model.py into a compact binary file (`models/flowclf.iqm`). This module evaluates it with
the Python standard library only: no NumPy, no scikit-learn, no joblib. That is what lets the engine
run on an OpenWrt/prplOS gateway, where those packages are not available or too large.

File format (little-endian):
    b"IQM1" | u32 header_len | header JSON (utf-8) | packed arrays
The header lists the classes, features, conformal threshold and, for every calibrated member,
its trees (node count) and its per-class isotonic calibrators. Each tree is stored as four arrays:
feature (int16, -1 = leaf), threshold (float64), left/right child (int32), plus a leaf table of
class fractions (float32, one row per node).

Exactness: tests/test_treemodel.py checks the probabilities against scikit-learn on held-out data.
"""
from __future__ import annotations

import bisect
import json
import struct
from array import array

MAGIC = b"IQM1"


class _Tree:
    __slots__ = ("feat", "thr", "left", "right", "val", "k")

    def __init__(self, feat, thr, left, right, val, k):
        self.feat, self.thr, self.left, self.right, self.val, self.k = feat, thr, left, right, val, k

    def leaf(self, x) -> int:
        feat, thr, left, right = self.feat, self.thr, self.left, self.right
        n = 0
        f = feat[0]
        while f >= 0:
            n = left[n] if x[f] <= thr[n] else right[n]
            f = feat[n]
        return n


class _Isotonic:
    __slots__ = ("x", "y")

    def __init__(self, x, y):
        self.x, self.y = list(x), list(y)

    def __call__(self, v: float) -> float:
        x, y = self.x, self.y
        if v <= x[0]:
            return y[0]
        if v >= x[-1]:
            return y[-1]
        i = bisect.bisect_right(x, v)
        x0, x1, y0, y1 = x[i - 1], x[i], y[i - 1], y[i]
        return y0 if x1 == x0 else y0 + (y1 - y0) * (v - x0) / (x1 - x0)


class TreeModel:
    """Same interface as the scikit-learn bundle wrapper: predict(X) -> [(label, prob, prediction_set)]."""

    def __init__(self, path: str):
        with open(path, "rb") as f:
            data = f.read()
        if data[:4] != MAGIC:
            raise ValueError(f"{path} is not an IntentQoS model file")
        (hl,) = struct.unpack_from("<I", data, 4)
        hdr = json.loads(data[8:8 + hl].decode())
        off = 8 + hl
        self.classes: list[str] = hdr["classes"]
        self.features: list[str] = hdr["features"]
        self.qhat = float(hdr["qhat"])
        self.alpha = float(hdr["alpha"])
        self.meta = hdr.get("meta", {})
        self.path = path
        k = len(self.classes)
        self.members = []
        for m in hdr["members"]:
            trees = []
            for nn in m["trees"]:
                feat = array("h"); feat.frombytes(data[off:off + 2 * nn]); off += 2 * nn
                thr = array("d"); thr.frombytes(data[off:off + 8 * nn]); off += 8 * nn
                left = array("i"); left.frombytes(data[off:off + 4 * nn]); off += 4 * nn
                right = array("i"); right.frombytes(data[off:off + 4 * nn]); off += 4 * nn
                val = array("f"); val.frombytes(data[off:off + 4 * nn * k]); off += 4 * nn * k
                trees.append(_Tree(feat, thr, left, right, val, k))
            cals = [_Isotonic(c["x"], c["y"]) for c in m["calibrators"]]
            self.members.append((trees, cals))
        if off != len(data):
            raise ValueError("model file has trailing or missing bytes")

    def proba(self, x) -> list[float]:
        k = len(self.classes)
        acc = [0.0] * k
        for trees, cals in self.members:
            raw = [0.0] * k
            for t in trees:
                b = t.leaf(x) * k
                v = t.val
                for c in range(k):
                    raw[c] += v[b + c]
            nt = len(trees)
            p = [cals[c](raw[c] / nt) for c in range(k)]
            s = sum(p)
            p = [1.0 / k] * k if s == 0 else [min(1.0, v / s) if v / s <= 1.0 + 1e-5 else v / s for v in p]
            for c in range(k):
                acc[c] += p[c]
        n = len(self.members)
        return [a / n for a in acc]

    def predict(self, X):
        out = []
        for x in X:
            p = self.proba(x)
            pset = [self.classes[i] for i, pi in enumerate(p) if 1.0 - pi <= self.qhat]
            j = max(range(len(p)), key=p.__getitem__)
            out.append((self.classes[j], float(p[j]), pset))
        return out
