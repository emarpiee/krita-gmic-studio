"""Filesystem locations. Qt-free so it can be unit-tested."""
import os
import sys

APP_DIR = "gmic_studio"


def config_dir():
    """Per-user folder holding settings, presets and the optional private gmic copy."""
    override = os.environ.get("GMIC_STUDIO_HOME")
    if override:
        base = override
    elif sys.platform.startswith("win"):
        base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "krita", APP_DIR)
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~/Library/Application Support"), "krita", APP_DIR)
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        base = os.path.join(xdg, "krita", APP_DIR)
    os.makedirs(base, exist_ok=True)
    return base


def private_bin_dir():
    path = os.path.join(config_dir(), "bin")
    os.makedirs(path, exist_ok=True)
    return path


def exe_name():
    return "gmic.exe" if sys.platform.startswith("win") else "gmic"


def gmic_config_dirs():
    """Folders where G'MIC itself caches downloaded filter definitions (update*.gmic)."""
    dirs = []
    env = os.environ.get("GMIC_PATH")
    if env:
        dirs.append(env)
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if appdata:
            dirs.append(os.path.join(appdata, "gmic"))
    dirs.append(os.path.expanduser("~/.config/gmic"))
    dirs.append(os.path.expanduser("~"))
    return [d for d in dirs if os.path.isdir(d)]
