#!/usr/bin/env python3
"""Run the whole test sequence gently: one small step at a time, lowest priority, stop at the first failure.

Steps: test_core.py, then test_plugin.py in small batches (each batch is its own pytest process, so memory is
freed between batches), then tools/smoke_gmic.py (needs a real gmic).

    python tools/run_tests.py                 # everything
    python tools/run_tests.py --batch 10      # smaller batches
    python tools/run_tests.py --no-smoke      # skip the real-gmic check
"""
import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cmd, timeout):
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    return subprocess.run(cmd, cwd=ROOT, env=env, timeout=timeout,
                          preexec_fn=lambda: os.nice(19) if hasattr(os, "nice") else None)


def collect(path):
    out = subprocess.run([sys.executable, "-m", "pytest", path, "--collect-only", "-q"], cwd=ROOT,
                         capture_output=True, text=True,
                         env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "DISPLAY": ""})
    return [line for line in out.stdout.splitlines() if "::" in line]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=20, help="tests per pytest process (default 20)")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds to rest between steps (default 1)")
    ap.add_argument("--no-smoke", action="store_true", help="skip tools/smoke_gmic.py")
    args = ap.parse_args()

    steps = [("test_core.py", [sys.executable, "-m", "pytest", "-q", "-x", "tests/test_core.py"])]
    ids = collect("tests/test_plugin.py")
    if not ids:
        sys.exit("could not collect tests/test_plugin.py (is PyQt6 installed? try: pip install pytest PyQt6)")
    for i in range(0, len(ids), args.batch):
        chunk = ids[i:i + args.batch]
        steps.append((f"test_plugin.py [{i + 1}-{i + len(chunk)} of {len(ids)}]",
                      [sys.executable, "-m", "pytest", "-q", "-x", *chunk]))
    if not args.no_smoke:
        steps.append(("smoke_gmic.py", [sys.executable, "tools/smoke_gmic.py"]))

    for n, (name, cmd) in enumerate(steps, 1):
        print(f"\n== step {n}/{len(steps)}: {name}", flush=True)
        try:
            ok = run(cmd, timeout=600).returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
            print("timed out after 600 s")
        if not ok:
            sys.exit(f"FAILED at step {n}: {name}")
        time.sleep(args.pause)
    print("\nall steps passed")


if __name__ == "__main__":
    main()
