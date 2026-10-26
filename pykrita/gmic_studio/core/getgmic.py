"""Getting a `gmic` program: official Windows download, or the right package-manager command elsewhere.

Network use happens only in `windows_zip_url()` / `download()`, and only when the user clicks.
"""
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request

PAGE_URL = "https://gmic.eu/download.html"
_HOST = "gmic.eu"
_ZIP_RE = re.compile(r"get_file\.php\?file=(windows/gmic_[0-9.]+_cli_win64\.zip)")
FALLBACK_ZIP = "https://gmic.eu/get_file.php?file=prerelease/gmic_cli_win64.zip"
MAX_BYTES = 400 * 1024 * 1024
_UA = {"User-Agent": "krita-gmic-studio"}


class DownloadError(Exception):
    pass


def can_download():
    """Only Windows has an official standalone CLI archive."""
    return sys.platform.startswith("win")


def _check(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != _HOST:
        raise DownloadError("refusing to download from %s" % url)


def windows_zip_url(timeout=20):
    """Newest stable CLI zip linked from gmic.eu (falls back to the stable-named dev build)."""
    _check(PAGE_URL)
    try:
        req = urllib.request.Request(PAGE_URL, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            page = resp.read(2_000_000).decode("utf-8", "replace")
    except OSError as exc:
        raise DownloadError("could not reach gmic.eu: %s" % exc)
    m = _ZIP_RE.search(page)
    return "https://gmic.eu/get_file.php?file=" + m.group(1) if m else FALLBACK_ZIP


def download(url, progress=None, cancelled=lambda: False, timeout=30):
    """Download `url` to a temp file and return its path. progress(done, total_or_0)."""
    _check(url)
    fd, path = tempfile.mkstemp(suffix=".zip", prefix="gmic_dl_")
    try:
        req = urllib.request.Request(url, headers=_UA)
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(req, timeout=timeout) as resp:
            _check(resp.geturl())
            total = int(resp.headers.get("Content-Length") or 0)
            if total > MAX_BYTES:
                raise DownloadError("download is unreasonably large")
            done = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_BYTES:
                    raise DownloadError("download is unreasonably large")
                if cancelled():
                    raise DownloadError("cancelled")
                out.write(chunk)
                if progress:
                    progress(done, total)
        return path
    except OSError as exc:
        _remove(path)
        raise DownloadError(str(exc))
    except BaseException:
        _remove(path)
        raise


def _remove(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def os_release(path="/etc/os-release"):
    info = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                k, sep, v = line.strip().partition("=")
                if sep:
                    info[k] = v.strip('"')
    except OSError:
        pass
    return info


def install_command(platform=None, release=None):
    """(description, shell command) for installing gmic on this system, or (description, None)."""
    platform = platform or sys.platform
    if platform == "darwin":
        return "Install G'MIC with Homebrew:", "brew install gmic"
    if platform.startswith("win"):
        return "Use the download button, or install with winget/Scoop.", None
    rel = release if release is not None else os_release()
    ids = set(filter(None, [rel.get("ID", "")] + rel.get("ID_LIKE", "").split()))
    table = (
        ({"fedora", "rhel", "centos"}, "sudo dnf install --setopt=install_weak_deps=False gmic"),
        ({"debian", "ubuntu"}, "sudo apt install gmic"),
        ({"arch", "manjaro"}, "sudo pacman -S gmic"),
        ({"opensuse", "suse", "opensuse-tumbleweed", "opensuse-leap"}, "sudo zypper install gmic"),
        ({"alpine"}, "sudo apk add gmic"),
        ({"void"}, "sudo xbps-install gmic"),
        ({"gentoo"}, "sudo emerge media-gfx/gmic"),
    )
    for names, cmd in table:
        if ids & names:
            return "Install G'MIC with your package manager (run in a terminal):", cmd
    return "Install the `gmic` package with your distribution's package manager.", None
