import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pykrita"))
sys.path.insert(0, os.path.join(ROOT, "tests", "stubs"))   # fake `krita` module
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
