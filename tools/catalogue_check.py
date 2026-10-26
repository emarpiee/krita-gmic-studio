#!/usr/bin/env python3
"""Run EVERY filter in the catalogue with default parameters through the real gmic.

    python3 tools/catalogue_check.py [--jobs 4] [--limit N] [--out report.json]

Reports filters that fail, time out, or change the image size, grouped by error. Needs gmic
(no Krita). Failures from filters newer than your gmic ("unknown command") are expected.
"""
import argparse
import collections
import concurrent.futures as cf
import json
import os
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pykrita", "gmic_studio"))
os.environ.setdefault("GMIC_STUDIO_HOME", os.path.join(ROOT, "dist", "check_home"))

from core import engine, filters  # noqa: E402

W = H = 48


def test_image():
    px = bytearray()
    for y in range(H):
        for x in range(W):
            px += bytes((x * 5 % 256, y * 5 % 256, (x + y) * 3 % 256, 255 if x > 4 else 128))
    return bytes(px)


def classify(msg):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    for pat, name in ((r"nknown command|nknown function", "unknown command (gmic too old?)"),
                      (r"Undefined argument", "wrong argument count"),
                      (r"No display|display", "needs a display"),
                      (r"timed out", "timeout"),
                      (r"nvalid|rror", "other gmic error")):
        if re.search(pat, msg):
            return name
    return "other"


def check(exe, fdef, timeout):
    t0 = time.time()
    try:
        command, frags = fdef.command_line()
        job = engine.Job(command, frags, W, H, test_image(), 8, None, "auto")
        w, h, depth, path, workdir = engine.Runner(exe, job, timeout=timeout).run()
        shutil.rmtree(workdir, ignore_errors=True)
        status = "ok" if (w, h) == (W, H) else "size changed"
        return fdef.id, status, "%dx%d" % (w, h), time.time() - t0
    except engine.GmicError as exc:
        text = str(exc)
        return fdef.id, classify(text), re.sub(r"\x1b\[[0-9;]*m", "", text)[-300:], time.time() - t0
    except Exception as exc:  # parser/serializer bug
        return fdef.id, "plugin error", repr(exc), time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--every", type=int, default=1, help="test only every Nth filter (quick sample)")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--out", default=os.path.join(ROOT, "dist", "catalogue_report.json"))
    args = ap.parse_args()
    os.nice(19)                                  # keep the desktop responsive
    exe = engine.locate()
    if not exe:
        sys.exit("gmic not found")
    index = filters.FilterIndex.load()
    todo = [f for f in index.by_id.values()]
    todo = sorted(todo, key=lambda f: f.id)[::max(1, args.every)]
    if args.limit:
        todo = todo[:args.limit]
    print("gmic %s, %d filters from %s" % (engine.version(exe), len(todo), index.sources or "built-in set"), flush=True)
    results, done = [], 0
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    stream = open(args.out + "l", "w")           # .jsonl: partial results survive an interrupted run
    with cf.ThreadPoolExecutor(args.jobs) as pool:
        for res in pool.map(lambda f: check(exe, f, args.timeout), todo):
            results.append(res)
            stream.write(json.dumps(dict(id=res[0], status=res[1], detail=res[2], seconds=round(res[3], 2))) + "\n")
            stream.flush()
            done += 1
            if done % 100 == 0:
                print("  %d/%d" % (done, len(todo)), flush=True)
    counts = collections.Counter(r[1] for r in results)
    print("\nSummary:")
    for name, n in counts.most_common():
        print("  %5d  %s" % (n, name))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump([dict(id=r[0], status=r[1], detail=r[2], seconds=round(r[3], 2)) for r in results], fh, indent=1)
    print("report:", args.out)


if __name__ == "__main__":
    main()
