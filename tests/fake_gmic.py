#!/usr/bin/env python3
"""Stand-in for the gmic binary: records its argv, copies in.png to out.png."""
import json
import shutil
import sys
import time

argv = sys.argv[1:]
if argv[:1] == ["version"]:
    print("3.9.9")
    sys.exit(0)
if argv[:1] == ["update"]:
    print("updated")
    sys.exit(0)
with open("argv.json", "w") as fh:
    json.dump(argv, fh)
script = open("job.gmic").read() if "job.gmic" in argv else ""
if "sleep[" in script:
    time.sleep(30)
if "needsrgb[" in script and "split_opacity" not in script:
    print("[afre]>_<) softlight: Select image(s) with 1 or 3 channels (I or RGB).", file=sys.stderr)
    sys.exit(1)
if "fail[" in script:
    print("[gmic]-0./ *** Error *** boom", file=sys.stderr)
    sys.exit(1)
shutil.copy("in.png", "out.png")
