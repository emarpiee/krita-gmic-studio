"""Locating and driving the `gmic` command-line binary. No Qt, no Krita."""
import dataclasses
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field

from . import filters, log, paths, pngio

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# gmic errors that mean "this filter cannot take an image with an alpha channel"
_NEEDS_RGB_RE = re.compile(r"channels?\b|spectrum", re.I)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)*")


class GmicError(Exception):
    pass


class Cancelled(GmicError):
    pass


# -- locating -------------------------------------------------------------
_LIBPATH_VARS = ("LD_LIBRARY_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH",
                 "DYLD_INSERT_LIBRARIES")


def clean_env(env=None):
    """Environment for running an external gmic.

    Also removes display variables on Linux so filters can never open windows.

    Krita's AppImage puts its own bundled libraries (older libssl, libstdc++, ...) on the library path, and a
    system gmic would load those and fail. Drop every entry that points inside the running AppImage/bundle.
    """
    env = dict(os.environ if env is None else env)
    if sys.platform.startswith("linux"):
        # Some filters call G'MIC's `display`/interactive windows. With no display they fail with an
        # error instead of popping windows up from a background process.
        for var in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"):
            env.pop(var, None)
    appdir = env.get("APPDIR", "")
    for var in _LIBPATH_VARS:
        if var not in env:
            continue
        keep = [e for e in env[var].split(os.pathsep)
                if e and ".mount_" not in e and not (appdir and e.startswith(appdir))]
        if keep:
            env[var] = os.pathsep.join(keep)
        else:
            del env[var]
    return env


def _is_exe(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def locate(configured=""):
    """Return the gmic executable to use, or None. Order: setting, private copy, PATH, usual places."""
    if _is_exe(configured):
        return configured
    private = os.path.join(paths.private_bin_dir(), paths.exe_name())
    if _is_exe(private):
        return private
    for hit in sorted(glob.glob(os.path.join(paths.private_bin_dir(), "*", paths.exe_name()))):
        if _is_exe(hit):
            return hit
    found = shutil.which("gmic")
    if found:
        return found
    candidates = ["/usr/bin/gmic", "/usr/local/bin/gmic", "/opt/homebrew/bin/gmic", "/opt/local/bin/gmic"]
    if sys.platform.startswith("win"):
        for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if root:
                candidates += glob.glob(os.path.join(root, "gmic*", "gmic.exe"))
    for cand in candidates:
        if _is_exe(cand):
            return cand
    return None


def _run_capture(args, timeout):
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, creationflags=_NO_WINDOW, env=clean_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GmicError(str(exc))
    return proc.returncode, _ANSI_RE.sub("", proc.stdout.decode("utf-8", "replace"))


def version(exe):
    """Version string reported by the binary, or raise GmicError if it does not work."""
    code, out = _run_capture([exe, "version"], 30)
    if code != 0:
        raise GmicError("`gmic version` failed:\n" + out.strip()[-400:])
    m = _VERSION_RE.search(out)
    return m.group(0) if m else "unknown"


def update_definitions(exe):
    """Download G'MIC's latest filter definitions. This is the only network use, user-triggered."""
    code, out = _run_capture([exe, "update"], 300)
    if code != 0:
        raise GmicError("`gmic update` failed:\n" + out.strip()[-600:])
    return out.strip()


# -- jobs -----------------------------------------------------------------
@dataclass
class Job:
    command: str
    arguments: list = field(default_factory=list)   # already-serialized fragments
    width: int = 0
    height: int = 0
    data: bytes = b""            # tightly packed RGBA, 8-bit or big-endian 16-bit
    depth: int = 8
    mask: bytes = None           # tightly packed 8-bit gray, same size, or None
    alpha_mode: str = "auto"     # "filter": RGBA goes through the filter (what Krita's G'MIC does);
                                 # "preserve": filter RGB only, keep original opacity;
                                 # "auto": "filter", retried as "preserve" if the filter rejects 4 channels
    definitions: str = ""        # extra definition file passed with `-m`
    seed: int = None             # random seed, so preview and Apply of a random filter agree
    bg_probes: list = None       # [(x, y)] transparent pixels used to detect & remove a filter's background fill


SCRIPT_NAME = "job.gmic"
JOB_COMMAND = "studio_job"


BG_SPREAD = 24       # how much the probes may differ and still count as one background (noisy fills are fine)
BG_NEAR = 24         # a pixel this close to the background color (0..255) is background through and through...
BG_FAR = 64          # ...and one this far from it is content; in between it fades
CHANGED = 12         # how much a pixel must differ from the input to count as "changed by the filter"


def centered(new, old):
    """Offset of the old image inside a canvas of size `new`, the way G'MIC's centered resize places it."""
    return int((new - old) / 2)               # truncates toward zero, like CImg


def _bg_removal(t, probes):
    """Script steps that make a filter's background fill transparent. One general rule, no per-filter cases.

    1. Find the fill: at the probe pixels, which were transparent in the input, the output must be (roughly)
       one and the same opaque color. That is the filter's background. (No such color -> nothing is done.)
    2. A pixel is background when it is close to that color AND it is either empty in the input or was changed
       by the filter. Pixels the filter left as they were (a black pupil in the artwork) stay.
    3. Clearing is soft ("color to alpha"): fully transparent near the fill color, fading to opaque further
       away, with the color scaled back out of the fill, so edges are clean and keep no halo.

    The input is taken to sit centered in the output, so frames and borders added around it count as empty.
    """
    n, k = t + 1, len(probes)
    matte = n + 4
    ox, oy = "int((w#%d-w#0)/2)" % t, "int((h#%d-h#0)/2)" % t
    steps = []
    for x, y in probes:
        steps += ["+crop[%d]" % t, "{%d+%s},{%d+%s},{%d+%s},{%d+%s}" % (x, ox, y, oy, x, ox, y, oy)]
    steps += ["append[%d-%d]" % (n, n + k - 1), "x", "split[%d]" % n, "c"]
    cond = "&&".join("iM#%d-im#%d<=%d" % (n + c, n + c, BG_SPREAD) for c in range(3)) + "&&im#%d>=254" % (n + 3)
    bg = ["i(#%d,0,0,0,0)" % (n + c) for c in range(3)]
    # stage 1: how opaque each pixel should be (one value per pixel)
    stage1 = (
        "sr=i(#{T},x,y,0,0);sg=i(#{T},x,y,0,1);sb=i(#{T},x,y,0,2);"
        "d=max(abs(sr-{B0}),abs(sg-{B1}),abs(sb-{B2}));"
        "chg=max(abs(sr-i(#0,x,y,0,0)),abs(sg-i(#0,x,y,0,1)),abs(sb-i(#0,x,y,0,2)));"
        "(i(#0,x,y,0,3)<1||chg>{CHG})?min(1,max(0,(d-{NEAR})/{SPAN})):1"
    ).format(T=t, B0=bg[0], B1=bg[1], B2=bg[2], CHG=CHANGED, NEAR=BG_NEAR, SPAN=BG_FAR - BG_NEAR)
    # stage 2: apply it: scale the opacity, and take the fill color back out of partly transparent pixels
    stage2 = (
        "a=i(#{M},x,y,0,0);bc=c==0?{B0}:(c==1?{B1}:{B2});"
        "c==3?i*a:(a<1?min(255,max(0,bc+(i-bc)/max(a,0.05))):i)"
    ).format(M=matte, B0=bg[0], B1=bg[1], B2=bg[2])
    steps += ["if", "{%s}" % cond,
              "resize[%d-%d]" % (n, n + 2), "1,1,1,1,2",                                  # mean of the probes = the fill color
              "if", "{w#0!=w#%d||h#0!=h#%d}" % (t, t), "resize[0]", "{w#%d},{h#%d},1,4,0,0,0.5,0.5" % (t, t), "fi",
              "[%d]" % t, "channels[%d]" % matte, "3",                                    # a 1-channel image to hold the opacity
              "fill[%d]" % matte, '"%s"' % stage1,
              "fill[%d]" % t, '"%s"' % stage2,
              "remove[%d]" % matte,
              "fi", "remove[%d-%d]" % (n, n + 3)]
    return steps


def build_script(job, in_name="in.png", mask_name="mask.png", out_name="out.png"):
    """The whole pipeline as one G'MIC custom command.

    It is read from a file rather than passed as command-line arguments: G'MIC re-quotes argv items that
    contain spaces, which breaks any filter taking text with spaces. Only relative names are used (cwd is
    the job folder).
    """
    if not filters.COMMAND_RE.match(job.command):
        raise GmicError("illegal command name %r" % job.command)
    preserve = job.alpha_mode == "preserve"
    probes = list(job.bg_probes or []) if not preserve else []
    if len(probes) < 2:
        probes = []
    steps = []
    if job.seed is not None:
        steps += ["srand", str(int(job.seed))]
    steps += ["-input", in_name]
    if job.mask is not None:
        steps += ["-input", mask_name]
    if job.depth == 16:
        steps += ["div[0]", "257"]                # filters always see the familiar 0..255 range
    t = 0
    if job.mask is not None or probes:
        steps += ["[0]"]                          # keep the original; the filter works on a copy
        t = 2 if job.mask is not None else 1
    if preserve:
        steps += ["split_opacity[%d]" % t]
    steps += ["%s[%d]" % (job.command, t)]
    if job.arguments:
        steps += [",".join(job.arguments)]
    if preserve:
        # If the filter changed the image size, the old opacity no longer fits: keep the result opaque.
        steps += ["if", "{w#%d==w#%d&&h#%d==h#%d}" % (t, t + 1, t, t + 1),
                  "to_rgb[%d]" % t, "append[%d,%d]" % (t, t + 1), "c",
                  "else", "to_rgba[%d]" % t, "remove[%d]" % (t + 1), "fi"]
    else:
        steps += ["to_rgba[%d]" % t]
    steps += ["cut[%d]" % t, "0,255"]
    if probes:
        steps += _bg_removal(t, probes)
    if job.mask is not None:
        # Blend through the selection mask, unless the filter resized the image (then there is nothing to blend).
        steps += ["if", "{w#2==w#0&&h#2==h#0}",
                  "div[1]", "255", "sub[2]", "[0]", "mul[2]", "[1]", "add[2]", "[0]", "fi"]
    if job.mask is not None or probes:
        steps += ["keep[%d]" % t]
    if job.depth == 16:
        steps += ["mul[0]", "257", "round[0]", "cut[0]", "0,65535", "output[0]", out_name + ",2"]
    else:
        steps += ["round[0]", "output[0]", out_name]
    return "%s : %s\n" % (JOB_COMMAND, " ".join(steps))


def build_args(exe, job):
    args = [exe]
    if job.definitions:
        args += ["-m", job.definitions]
    return args + ["-m", SCRIPT_NAME, JOB_COMMAND]


class Runner:
    """Runs one job in a private temp folder. `cancel()` may be called from any thread."""

    def __init__(self, exe, job, timeout=600):
        self.exe, self.job, self.timeout = exe, job, timeout
        self._proc = None
        self._cancelled = False
        self._lock = threading.Lock()

    def cancel(self):
        with self._lock:
            self._cancelled = True
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.kill()
                except OSError:
                    pass

    def run(self):
        """Blocks; returns (width, height, depth, path_to_png, workdir). Caller removes workdir."""
        try:
            return self._run_once(self.job)
        except GmicError as exc:
            if isinstance(exc, Cancelled) or self.job.alpha_mode != "auto" or not _NEEDS_RGB_RE.search(str(exc)):
                raise
        return self._run_once(dataclasses.replace(self.job, alpha_mode="preserve"))

    def _run_once(self, job):
        workdir = tempfile.mkdtemp(prefix="gmic_studio_")
        started = time.time()
        log.get().info("run %s %s %dx%d depth=%d alpha=%s mask=%s bg=%s seed=%s", job.command,
                       ",".join(job.arguments)[:300], job.width, job.height, job.depth, job.alpha_mode,
                       job.mask is not None, bool(job.bg_probes), job.seed)
        try:
            pngio.write(os.path.join(workdir, "in.png"), job.width, job.height, job.data, pngio.RGBA, job.depth)
            if job.mask is not None:
                pngio.write(os.path.join(workdir, "mask.png"), job.width, job.height, job.mask, pngio.GRAY, 8)
            with open(os.path.join(workdir, SCRIPT_NAME), "w", encoding="utf-8") as fh:
                fh.write(build_script(job))
            args = build_args(self.exe, job)
            with self._lock:
                if self._cancelled:
                    raise Cancelled()
                try:
                    self._proc = subprocess.Popen(args, cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                  stderr=subprocess.STDOUT, creationflags=_NO_WINDOW,
                                                  env=clean_env())
                except OSError as exc:
                    raise GmicError("could not start gmic: %s" % exc)
            try:
                out, _ = self._proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.communicate()
                raise GmicError("gmic timed out after %d s" % self.timeout)
            if self._cancelled:
                raise Cancelled()
            result = os.path.join(workdir, "out.png")
            if self._proc.returncode != 0 or not os.path.isfile(result):
                text = _ANSI_RE.sub("", out.decode("utf-8", "replace"))
                tail = "\n".join(text.strip().splitlines()[-8:])
                raise GmicError(tail or "gmic exited with code %s" % self._proc.returncode)
            w, h, depth, ctype = pngio.read_header(result)
            log.get().info("done %s in %.2fs -> %dx%d", job.command, time.time() - started, w, h)
            return w, h, depth, result, workdir
        except BaseException as exc:
            if isinstance(exc, Cancelled):
                log.get().info("cancelled %s after %.2fs", job.command, time.time() - started)
            else:
                log.get().warning("failed %s after %.2fs: %s", job.command, time.time() - started, str(exc)[-400:])
            shutil.rmtree(workdir, ignore_errors=True)
            raise


def split_preview(exe, before_png, after_png, mode, x, y, timeout=60):
    """G'MIC's own split-view layouts (the "Duplicate ..." modes) made from two finished images.

    Returns the PNG file written next to the inputs. `mode` is G'MIC-Qt's preview-mode number, x/y the split
    point in percent. The filtered image is swapped in for the filter, so nothing is computed twice.
    """
    folder = os.path.dirname(before_png)
    script = ("studio_pick : -input \"%s\" -remove[0]\n"
              "studio_split : -input \"%s\" -gui_split_preview[0] \"studio_pick\",%d,%g,%g -output[0] \"%s\"\n"
              % (after_png.replace("\\", "/"), before_png.replace("\\", "/"), int(mode), x, y,
                 os.path.join(folder, "split.png").replace("\\", "/")))
    with open(os.path.join(folder, "split.gmic"), "w", encoding="utf-8") as fh:
        fh.write(script)
    try:
        proc = subprocess.run([exe, "-m", "split.gmic", "studio_split"], cwd=folder, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                              creationflags=_NO_WINDOW, env=clean_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise GmicError("split preview failed: %s" % exc)
    out = os.path.join(folder, "split.png")
    if proc.returncode != 0 or not os.path.isfile(out):
        raise GmicError("split preview failed: %s" % proc.stdout.decode("utf-8", "replace")[-200:])
    return out
