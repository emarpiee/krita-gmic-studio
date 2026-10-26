#!/usr/bin/env python3
"""Check the plugin's gmic pipeline against a REAL gmic binary (no Krita needed).

    python3 tools/smoke_gmic.py [/path/to/gmic]

Exercises 8/16-bit, both transparency modes, and a selection mask, and prints the gmic
command line used for each case so a failure is easy to reproduce by hand.
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pykrita", "gmic_studio"))   # import `core` alone, without Krita
os.environ.setdefault("GMIC_STUDIO_HOME", os.path.join(ROOT, "dist", "smoke_home"))

from core import engine  # noqa: E402

W, H = 32, 24


def image(depth):
    px = bytearray()
    for y in range(H):
        for x in range(W):
            r, g, b, a = x * 255 // (W - 1), y * 255 // (H - 1), 128, 255 - x * 3
            px += bytes((r, g, b, a)) if depth == 8 else b"".join(v.to_bytes(2, "big") for v in (r * 257, g * 257, b * 257, a * 257))
    return bytes(px)


def main():
    exe = engine.locate(sys.argv[1] if len(sys.argv) > 1 else "")
    if not exe:
        sys.exit("gmic not found; pass its path as an argument")
    print("gmic:", exe, engine.version(exe))
    failures = 0
    for depth in (8, 16):
        for alpha in ("auto", "preserve", "filter"):
            for masked in (False, True):
                job = engine.Job("blur", ["2"], W, H, image(depth), depth,
                                 bytes([255 if x < W // 2 else 0 for y in range(H) for x in range(W)]) if masked else None, alpha)
                label = "depth=%d alpha=%s mask=%s" % (depth, alpha, masked)
                try:
                    w, h, d, path, workdir = engine.Runner(exe, job, timeout=120).run()
                    ok = (w, h, d) == (W, H, depth)
                    shutil.rmtree(workdir, ignore_errors=True)
                    print(("ok   " if ok else "BAD  ") + label, "-> %dx%d %d-bit" % (w, h, d))
                    failures += not ok
                except engine.GmicError as exc:
                    failures += 1
                    print("FAIL " + label + "\n     " + str(exc).replace("\n", "\n     "))
                    print("     argv:", " ".join(engine.build_args(exe, job)))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
