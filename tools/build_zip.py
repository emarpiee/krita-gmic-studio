#!/usr/bin/env python3
"""Build dist/krita-gmic_studio-plugin.zip, installable via Tools > Scripts > Import Python Plugin from File."""
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "pykrita")
OUT = os.path.join(ROOT, "dist", "krita-gmic_studio-plugin.zip")


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, dirs, files in os.walk(SRC):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            if base != SRC:  # explicit directory entries, like `zip -r`; Krita's importer lists them
                zf.writestr(os.path.relpath(base, SRC) + "/", "")
            for name in sorted(files):
                if not name.endswith(".pyc"):
                    full = os.path.join(base, name)
                    zf.write(full, os.path.relpath(full, SRC))
    print(OUT)


if __name__ == "__main__":
    main()
