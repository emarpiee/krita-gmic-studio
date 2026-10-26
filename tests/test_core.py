import json
import os
import shutil
import stat
import struct
import sys
import tarfile
import threading
import time
import zipfile
import zlib

import pytest

from gmic_studio.core import engine, filters, install, paths, pixels, pngio
from gmic_studio.core.settings import Settings

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GMIC_STUDIO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))      # keep the real G'MIC config out of tests
    monkeypatch.delenv("GMIC_PATH", raising=False)
    return tmp_path


@pytest.fixture
def fake(tmp_path):
    path = tmp_path / "gmic"
    path.write_text("#!%s\n" % sys.executable + open(os.path.join(HERE, "fake_gmic.py")).read().split("\n", 1)[1])
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


# -- png / pixels -----------------------------------------------------------
def decode(png):
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, hdr = 8, b"", None
    while pos < len(png):
        n, tag = struct.unpack(">I4s", png[pos:pos + 8])
        body = png[pos + 8:pos + 8 + n]
        assert zlib.crc32(tag + body) & 0xFFFFFFFF == struct.unpack(">I", png[pos + 8 + n:pos + 12 + n])[0]
        if tag == b"IHDR":
            hdr = struct.unpack(">IIBB", body[:10])
        elif tag == b"IDAT":
            idat += body
        pos += 12 + n
    w, h, depth, ctype = hdr
    raw = zlib.decompress(idat)
    stride = w * {0: 1, 6: 4}[ctype] * depth // 8
    rows = [raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)] for y in range(h)]
    assert all(raw[y * (stride + 1)] == 0 for y in range(h))
    return w, h, depth, ctype, b"".join(rows)


@pytest.mark.parametrize("ctype,depth,ch", [(6, 8, 4), (6, 16, 4), (0, 8, 1)])
def test_png_roundtrip(ctype, depth, ch, tmp_path):
    w, h = 5, 3
    data = bytes(range(w * h * ch * depth // 8))
    f = tmp_path / "a.png"
    pngio.write(str(f), w, h, data, ctype, depth)
    assert pngio.read_header(str(f)) == (w, h, depth, ctype)
    assert decode(f.read_bytes()) == (w, h, depth, ctype, data)


def test_png_rejects_bad_buffer():
    with pytest.raises(ValueError):
        pngio.encode(2, 2, b"\x00" * 3)


def test_swap_rb_8_and_16():
    assert pixels.swap_rb(bytes([1, 2, 3, 4, 5, 6, 7, 8]), 1) == bytes([3, 2, 1, 4, 7, 6, 5, 8])
    px = bytes([1, 2, 3, 4, 5, 6, 7, 8])  # one 16-bit BGRA pixel
    assert pixels.swap_rb(px, 2) == bytes([5, 6, 3, 4, 1, 2, 7, 8])
    assert pixels.swap_rb(pixels.swap_rb(px, 2), 2) == px


def test_depth_conversions():
    le = bytes([0x34, 0x12, 0xFF, 0xFF])
    assert pixels.u16_to_u8(le) == bytes([0x12, 0xFF])
    assert pixels.u8_to_u16(bytes([0x12, 0xFF])) == bytes([0x12, 0x12, 0xFF, 0xFF])
    assert pixels.u16_native_to_be(le) == bytes([0x12, 0x34, 0xFF, 0xFF])


# -- definitions parser -----------------------------------------------------
SAMPLE = '''
#@gui Colors > <i>Fancy</i> Tone : fx_tone, fx_tone_preview(0)
#@gui : Amount = float(1.5,0,10)
#@gui : Steps = int(4,1,9)
#@gui : Mode = choice(1,"A","B, with comma","C")
#@gui : On = bool(true)
#@gui : Label = text(0,"he said \\"hi\\"")
#@gui : Tint = color(10,20,30,255)
#@gui : Where = point(25,75,0,0,255,0,0,255)
#@gui : sep = separator()
#@gui : n = note("hello")
#@gui_fr Couleurs > Teinte : fx_tone
#@gui Evil > Bad : fx_x; shell rm
#@gui : P = float(1,0,2)
#@gui Plain : -just_cmd
'''


def test_parse_sample():
    fs = filters.parse_definitions(SAMPLE)
    assert [f.id for f in fs] == ["Colors > Fancy Tone", "Plain"]
    f = fs[0]
    assert f.command == "fx_tone"
    kinds = [p.kind for p in f.params]
    assert kinds == ["float", "int", "choice", "bool", "text", "color", "point", "separator", "note"]
    assert f.params[2].choices == ["A", "B, with comma", "C"] and f.params[2].default == 1
    assert f.params[3].default is True
    assert f.params[4].default == 'he said "hi"'
    assert f.params[5].default == [10, 20, 30, 255]
    assert fs[1].command == "just_cmd"


def test_serialization_and_clamping():
    f = filters.parse_definitions(SAMPLE)[0]
    cmd, frags = f.command_line()
    assert cmd == "fx_tone"
    assert frags == ["1.5", "4", "1", "1", '"he said \\"hi\\""', "10,20,30,255", "25,75"]  # separator/note take no slot
    cmd, frags = f.command_line({"0": 999, "1": -5, "2": 99, "4": '"; shell ls; "'})
    assert frags[0] == "10" and frags[1] == "1" and frags[2] == "1"
    assert frags[4] == '"\\"; shell ls; \\""'


def test_quote_strips_control_chars():
    assert filters.quote("a\nb\x00c") == '"a\\nbc"'
    assert filters.quote("\\251 John \\9 x") == '"\\251 John \\\\9 x"'          # G'MIC's octal escape (the copyright sign) survives


def test_command_name_validated():
    bad = filters.FilterDef((), "x", "a b")
    with pytest.raises(ValueError):
        bad.command_line()


def test_builtin_filters_load_and_search():
    idx = filters.FilterIndex.load()
    assert idx.get("Basics > Blur").command == "blur"
    assert idx.search("blr")[0].name in ("Blur", "Directional Blur")
    assert [f.name for f in idx.search("sepia")] == ["Sepia"]
    assert idx.search("zzzzqq") == []


def test_find_definition_files_prefers_newest(tmp_path, monkeypatch):
    cfg = tmp_path / "gmicpath"
    cfg.mkdir()
    for n in ("update330.gmic", "update335.gmic", "update99.gmic"):
        (cfg / n).write_text("")
    monkeypatch.setenv("GMIC_PATH", str(cfg))
    assert filters.find_definition_files()[0].endswith("update335.gmic")
    explicit = tmp_path / "mine.gmic"
    explicit.write_text("")
    assert filters.find_definition_files(str(explicit)) == [str(explicit)]


# -- settings ---------------------------------------------------------------
def test_settings_persist_favorites_recents_and_per_filter_values():
    s = Settings()
    assert s.get("transparency") == "auto"
    s.toggle_favorite("A > B")
    s.remember_last("A > B", {"0": 1})
    s.remember_last("C > D", {"0": 5})
    s2 = Settings()
    assert s2.is_favorite("A > B") and s2.get("recents") == ["C > D", "A > B"]
    assert s2.values_for("A > B") == {"0": 1} and s2.values_for("C > D") == {"0": 5} and s2.values_for("X") is None


def test_settings_survive_corrupt_file():
    open(os.path.join(paths.config_dir(), "settings.json"), "w").write("{nope")
    assert Settings().get("preview_size") == 384


# -- engine -----------------------------------------------------------------
def job(**kw):
    base = dict(command="blur", arguments=["2"], width=2, height=1, data=bytes(8), alpha_mode="preserve")
    base.update(kw)
    return engine.Job(**base)


def test_script_plain_and_preserve():
    a = engine.build_script(job(alpha_mode="filter"))
    assert a == ("studio_job : -input in.png blur[0] 2 _studio_status=${} to_rgba[0] cut[0] 0,255 round[0] output[0] out.png "
                 "('S:${_studio_status}') output[-1] status.txt\n")
    a = engine.build_script(job()).split()
    assert "split_opacity[0]" in a and "append[0,1]" in a and a.index("split_opacity[0]") < a.index("blur[0]")


def test_script_16bit_with_mask_and_defs():
    a = engine.build_script(job(depth=16, mask=bytes(2), alpha_mode="filter")).split()
    assert a.index("div[0]") < a.index("[0]") < a.index("blur[2]") < a.index("keep[2]") < a.index("output[0]")
    assert "{w#2==w#0&&h#2==h#0}" in a and a.index("{w#2==w#0&&h#2==h#0}") < a.index("div[1]") < a.index("fi")
    assert a[-5:] == ["output[0]", "out.png,2", "('S:${_studio_status}')", "output[-1]", "status.txt"]   # the status goes last
    assert engine.build_args("gmic", job(definitions="d.gmic")) == ["gmic", "-m", "d.gmic", "-m", "job.gmic", "studio_job"]
    assert engine.build_args("gmic", job()) == ["gmic", "-m", "job.gmic", "studio_job"]


def test_script_rejects_bad_command():
    with pytest.raises(engine.GmicError):
        engine.build_script(job(command="blur; shell"))


def test_text_with_spaces_and_quotes_stays_one_script_line():
    arg = filters.quote('he said "hi" \\ and\nbye \\n done')
    script = engine.build_script(job(arguments=["1", arg]))
    assert script.count("\n") == 1 and arg in script        # a single line, text intact
    assert arg == '"he said \\"hi\\" \\\\ and\\nbye \\n done"'


def test_version_and_runner(fake):
    assert engine.version(fake) == "3.9.9"
    w, h, depth, path, workdir = engine.Runner(fake, job()).run()
    assert (w, h, depth) == (2, 1, 8) and os.path.isfile(path)
    assert "blur[0]" in open(os.path.join(workdir, "job.gmic")).read()
    assert json.load(open(os.path.join(workdir, "argv.json")))[-2:] == ["job.gmic", "studio_job"]


def test_runner_reports_failure_and_cleans_up(fake, tmp_path):
    with pytest.raises(engine.GmicError, match="boom"):
        engine.Runner(fake, job(command="fail", arguments=[])).run()


def test_runner_cancel(fake):
    r = engine.Runner(fake, job(command="sleep", arguments=[]))
    t0 = time.time()
    threading.Timer(0.5, r.cancel).start()
    with pytest.raises(engine.Cancelled):
        r.run()
    assert time.time() - t0 < 10


def test_locate_prefers_configured(fake):
    assert engine.locate(fake) == fake


# -- install ----------------------------------------------------------------
def test_install_zip_and_tar(tmp_path):
    name = paths.exe_name()
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("bundle/" + name, "#!/bin/sh\n")
    exe = install.install_archive(str(z))
    assert os.path.basename(exe) == name and os.access(exe, os.X_OK)
    src = tmp_path / name
    src.write_text("x")
    t = tmp_path / "a.tar.gz"
    with tarfile.open(t, "w:gz") as tf:
        tf.add(src, arcname="x/" + name)
    assert install.install_archive(str(t)).endswith(name)


def test_install_rejects_traversal_and_missing(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../../evil", "x")
    with pytest.raises(install.InstallError):
        install.install_archive(str(z))
    z2 = tmp_path / "empty.zip"
    with zipfile.ZipFile(z2, "w") as zf:
        zf.writestr("readme.txt", "x")
    with pytest.raises(install.InstallError, match="no "):
        install.install_archive(str(z2))
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"nope")
    with pytest.raises(install.InstallError):
        install.install_archive(str(junk))


# -- Krita's G'MIC-Qt cache ---------------------------------------------------
import struct as _st

from gmic_studio.core import getgmic, gmicqt_cache


def _s(text):
    b = text.encode()
    return _st.pack(">I", len(b)) + b


def _lst(items):
    return bytes([len(items)]) + b"".join(_s(i) for i in items)


def _cache_bytes(records, factors=None):
    """Builds a file in the same layout as G'MIC-Qt writes (first record: binary hash)."""
    out = b"\x030\x030\x00\x00\x00d"
    for n, (title, path, cmd, params) in enumerate(records):
        out += (b"\x00\x00\x00\x14" + bytes(20)) if n == 0 else (_s("%032x" % n) + b"\x00")
        out += _s(title) * 3
        out += _lst(["<b>%s</b>" % p for p in path]) + _lst(path) + _lst(path)
        out += _s(cmd) + _s(cmd + "_preview") + b"d" + _s(params)
        out += _st.pack(">d", (factors or {}).get(cmd, -1.0)) + b"\x01\x00"
    return out


def test_cache_parses_records_and_multiline_notes():
    params = ('_=note("line one\nline <a href="u">x</a>")\n_=separator()\nAmount=float(2,0,9)\n'
              'Mode=choice(1,"A &#40;x&#41;","B")\nOn=bool(1)')
    data = _cache_bytes([("First", ["Cat", "Sub"], "fx_first", params),
                         ("Second", ["Cat"], "fx_second", "N=int(4,1,9)"),
                         ("Bad name", ["Cat"], "not a command", "")])
    fs = gmicqt_cache.parse(data)
    assert [f.id for f in fs] == ["Cat > Sub > First", "Cat > Second"]
    assert [p.kind for p in fs[0].params] == ["note", "separator", "float", "choice", "bool"]
    assert fs[0].params[3].choices == ["A (x)", "B"]
    assert fs[0].params[0].default == 'line one\nline <a href="u">x</a>'
    assert fs[0].params[3].choices == ["A (x)", "B"]
    assert fs[0].command_line()[1] == ["2", "1", "1"]
    assert fs[1].command_line()[1] == ["4"]


def test_cache_survives_garbage_and_truncation():
    good = _cache_bytes([("A", ["C"], "fx_a", "X=int(1,0,2)"), ("B", ["C"], "fx_b", "Y=int(1,0,2)")])
    assert [f.command for f in gmicqt_cache.parse(good[:-40] + b"\xff" * 30)] == ["fx_a"]
    assert gmicqt_cache.parse(b"") == [] and gmicqt_cache.parse(b"junk" * 50) == []


def test_index_prefers_krita_cache_unless_gmics_own_definitions_are_newer(tmp_path, monkeypatch):
    cfg = tmp_path / "gm"
    cfg.mkdir()
    cache, update = cfg / gmicqt_cache.CACHE_NAME, cfg / "update400.gmic"
    cache.write_bytes(_cache_bytes([("Zed", ["Cat"], "fx_zed", "")]))
    update.write_text("#@gui Other > Nope : fx_nope\n")
    monkeypatch.setenv("GMIC_PATH", str(cfg))
    os.utime(update, (1000, 1000))
    os.utime(cache, (2000, 2000))
    idx = filters.FilterIndex.load()
    assert idx.get("Cat > Zed") and idx.get("Basics > Blur") and idx.get("Other > Nope") is None
    assert idx.sources == [str(cache)]
    os.utime(update, (3000, 3000))                  # gmic was updated after the cache was written: its parameter lists win
    idx = filters.FilterIndex.load()
    assert idx.get("Cat > Zed") is None and idx.get("Other > Nope") and idx.sources == [str(update)]


def test_real_krita_cache_if_present():
    real = os.path.expanduser("~/.config/gmic/" + gmicqt_cache.CACHE_NAME)
    if not os.path.isfile(real):
        pytest.skip("no G'MIC-Qt cache on this machine")
    fs, _ = gmicqt_cache.load(real)
    assert len(fs) > 500 and all(f.name and f.command for f in fs)


# -- getting gmic -----------------------------------------------------------
def test_install_command_by_distro():
    assert getgmic.install_command("linux", {"ID": "fedora"})[1] == "sudo dnf install --setopt=install_weak_deps=False gmic"
    assert getgmic.install_command("linux", {"ID": "pop", "ID_LIKE": "ubuntu debian"})[1] == "sudo apt install gmic"
    assert getgmic.install_command("linux", {"ID": "weird"})[1] is None
    assert getgmic.install_command("darwin", {})[1] == "brew install gmic"


def test_download_refuses_other_hosts():
    with pytest.raises(getgmic.DownloadError):
        getgmic.download("https://evil.example/gmic.zip")
    with pytest.raises(getgmic.DownloadError):
        getgmic.download("http://gmic.eu/get_file.php?file=x")


# -- AppImage library path -----------------------------------------------------
def test_clean_env_drops_appimage_libs():
    env = {"APPDIR": "/opt/app", "LD_LIBRARY_PATH": "/tmp/.mount_krita123/usr/lib:/opt/app/lib:/usr/local/lib",
           "LD_PRELOAD": "/tmp/.mount_krita123/x.so", "HOME": "/h"}
    out = engine.clean_env(env)
    assert out["LD_LIBRARY_PATH"] == "/usr/local/lib" and "LD_PRELOAD" not in out and out["HOME"] == "/h"
    assert "LD_LIBRARY_PATH" not in engine.clean_env({"LD_LIBRARY_PATH": "/tmp/.mount_x/usr/lib"})
    assert env["LD_PRELOAD"]                       # input is not mutated


def test_runner_uses_clean_env(fake, monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_krita9/usr/lib")
    recorded = {}
    real = engine.subprocess.Popen

    def spy(*a, **kw):
        recorded.update(kw["env"])
        return real(*a, **kw)

    monkeypatch.setattr(engine.subprocess, "Popen", spy)
    engine.Runner(fake, job()).run()
    assert "LD_LIBRARY_PATH" not in recorded


# -- parameter syntax variants seen in G'MIC's catalogue --------------------------
def test_param_prefixes_hex_colors_and_braced_notes():
    ps = filters.parse_param_block('X-Tiles=~int(5,1,20)\nA=_bool(1)\nTint=color(#c08040ff)\nBack=color(#102030)\n'
                                   'Num=color(1,2,3)\nIn=filein("")\nOut=fileout("")\nS=float(1,0,2)_0-\n_=note{"<b>hi</b>"}')
    assert [p.kind for p in ps] == ["int", "bool", "color", "color", "color", "file", "file", "float", "note"]
    assert ps[0].default == 5 and ps[2].default == [192, 128, 64, 255] and ps[3].default == [16, 32, 48]
    assert ps[4].default == [1, 2, 3] and ps[8].default == "<b>hi</b>"
    f = filters.FilterDef((), "x", "fx_x", ps)
    assert f.command_line()[1][:5] == ["5", "1", "192,128,64,255", "16,32,48", "1,2,3"]


def test_clean_env_removes_display_on_linux(monkeypatch):
    monkeypatch.setattr(engine.sys, "platform", "linux")
    out = engine.clean_env({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "XAUTHORITY": "/x", "HOME": "/h"})
    assert out == {"HOME": "/h"}


def test_centered_matches_gmics_rounding():
    assert [engine.centered(n, 2) for n in (2, 3, 4, 5, 6, 7)] == [0, 0, 1, 1, 2, 2]
    assert [engine.centered(2, o) for o in (3, 4, 5, 6)] == [0, -1, -1, -2]      # toward zero, as CImg does


def test_gmic_errors_have_no_ansi_codes(tmp_path):
    exe = tmp_path / "gmic"
    exe.write_text("#!%s\nimport sys\nprint('\\x1b[0;31;59m*** Error *** boom\\x1b[0;0m')\nsys.exit(1)\n" % sys.executable)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(engine.GmicError) as err:
        engine.Runner(str(exe), job()).run()
    assert "\x1b" not in str(err.value) and "boom" in str(err.value)


def test_param_block_survives_stray_quotes_and_keeps_multiline_notes():
    block = ('Mode=choice(0,"A","B",C")\nNext=float(1,0,2)\n_=note("line one\nline <a href="u">x</a>")\n'
             'Last=int(3,0,9)')
    ps = filters.parse_param_block(block)
    assert [p.kind for p in ps] == ["choice", "float", "note", "int"]
    assert ps[2].default == 'line one\nline <a href="u">x</a>' and ps[3].default == 3


def test_cache_reads_preview_zoom_factor():
    data = _cache_bytes([("A", ["C"], "fx_a", ""), ("B", ["C"], "fx_b", ""), ("D", ["C"], "fx_d", "")],
                        factors={"fx_a": 0.0, "fx_b": 1.0})
    assert [f.preview_factor for f in gmicqt_cache.parse(data)] == [0.0, 1.0, -1.0]


def test_seed_is_first_step_when_given():
    script = engine.build_script(job(seed=1234))
    assert script.startswith("studio_job : srand 1234 -input in.png")
    assert "srand" not in engine.build_script(job())


# -- transparency modes ------------------------------------------------------------
def test_auto_mode_passes_rgba_like_krita():
    auto = engine.build_script(job(alpha_mode="auto"))
    assert "split_opacity" not in auto and "to_rgba[0]" in auto
    assert engine.Job("blur").alpha_mode == "auto"


def test_auto_mode_retries_without_alpha_when_filter_needs_rgb(fake):
    w, h, depth, path, workdir = engine.Runner(fake, job(command="needsrgb", arguments=[], alpha_mode="auto")).run()
    assert "split_opacity[0]" in open(os.path.join(workdir, "job.gmic")).read()     # second attempt
    with pytest.raises(engine.GmicError, match="channels"):
        engine.Runner(fake, job(command="needsrgb", arguments=[], alpha_mode="filter")).run()
    w, h, depth, path, workdir = engine.Runner(fake, job(command="blur", alpha_mode="auto")).run()
    assert "split_opacity" not in open(os.path.join(workdir, "job.gmic")).read()    # no retry needed


def test_fit_rgba_crops_and_pads():
    px = bytes(range(1, 4 * 3 * 2 + 1))                       # 3x2 pixels, 4 bytes each
    assert pixels.fit_rgba(px, 3, 2, 3, 2, 4) == px
    cropped = pixels.fit_rgba(px, 3, 2, 2, 1, 4)
    assert cropped == px[:8]
    padded = pixels.fit_rgba(px, 3, 2, 4, 3, 4)
    assert len(padded) == 4 * 3 * 4 and padded[:12] == px[:12] and padded[12:16] == bytes(4)
    assert padded[16:28] == px[12:24] and padded[-16:] == bytes(16)


def test_value_parameter_passes_every_number():
    ps = filters.parse_param_block('_=value(50,50)\nP=point(10,20,0,1)_0\nB=button(0)\nH=hidden(7)')
    f = filters.FilterDef((), "x", "fx_x", ps)
    assert f.command_line()[1] == ["50,50", "10,20", "0", "7"]


# -- background-fill removal -----------------------------------------------------------
def _alpha_image(w, h, opaque):
    """RGBA bytes, transparent except the given (x, y) set."""
    return b"".join((b"\x10\x20\x30\xff" if (x, y) in opaque else b"\x00\x00\x00\x00") for y in range(h) for x in range(w))


def test_background_probes_need_some_content_and_some_transparency():
    w = h = 40
    assert pixels.background_probes(_alpha_image(w, h, set()), w, h, 4, 3) == []                  # empty layer: fill is wanted
    assert pixels.background_probes(_alpha_image(w, h, {(x, y) for x in range(w) for y in range(h)}), w, h, 4, 3) == []
    pts = pixels.background_probes(_alpha_image(w, h, {(20, 20)}), w, h, 4, 3)
    assert 3 <= len(pts) <= 6 and all(0 <= x < w and 0 <= y < h and (x, y) != (20, 20) for x, y in pts)
    be16 = b"".join((b"\x00" * 7 + b"\x00") if (x, y) != (1, 1) else b"\x12\x34\x12\x34\x12\x34\xff\xff"
                    for y in range(8) for x in range(8))
    assert pixels.background_probes(be16, 8, 8, 8, 6, grid=4) != []                                 # 16-bit (alpha high byte)


def test_script_removes_background_only_when_asked_and_possible():
    probes = [(3, 3), (30, 3), (3, 30)]
    s = engine.build_script(job(alpha_mode="auto", bg_probes=probes))
    t = s.split()
    assert t.index("[0]") < t.index("blur[1]") < t.index("+crop[1]") < t.index("fill[1]") < t.index("keep[1]")
    assert t.count("+crop[1]") == 3
    assert "{3+int((w#1-w#0)/2)},{3+int((h#1-h#0)/2)},{3+int((w#1-w#0)/2)},{3+int((h#1-h#0)/2)}" in t
    assert "{w#1==w#0&&h#1==h#0}" not in t                    # resized results are handled too (offset-aware)
    assert any("chg>12" in x and "i(#0,x,y,0,3)<1" in x for x in t)          # "empty in the input, or changed by the filter"
    assert any(x.startswith("{iM#2-im#2<=24") for x in t)                    # probes only need to agree roughly (noisy fills)
    assert t.count("fill[1]") == 1 and t.count("fill[%d]" % 6) == 1          # one pass for the opacity, one to apply it
    assert engine.build_script(job(alpha_mode="auto", bg_probes=[(1, 1)])) == engine.build_script(job(alpha_mode="auto"))
    assert engine.build_script(job(alpha_mode="preserve", bg_probes=probes)) == engine.build_script(job(alpha_mode="preserve"))
    both = engine.build_script(job(alpha_mode="auto", mask=bytes(2), bg_probes=probes)).split()
    assert both.index("blur[2]") < both.index("+crop[2]") < both.index("fill[2]") < both.index("keep[2]")


def test_forced_transparent_background_is_off_by_default():
    assert Settings().get("transparent_bg") is False                 # off on a fresh install: filters have their own options
    s = Settings()
    s.set("transparent_bg", True)
    assert Settings().get("transparent_bg") is True                  # a choice that was saved is kept


# -- preset library -------------------------------------------------------------------
from gmic_studio.core.presets import PresetStore


def test_presets_save_find_overwrite_rename_delete(tmp_path):
    st = PresetStore(str(tmp_path / "p.json"))
    a = st.save_preset("Soft", "Basics > Blur", {"0": 2}, seed=7)
    st.save_preset("Strong", "Basics > Blur", {"0": 20})
    st.save_preset("Soft", "Basics > Sharpen", {"0": 1})            # same name, other filter: separate
    assert [p["name"] for p in st.for_filter("Basics > Blur")] == ["Soft", "Strong"] and len(st) == 3
    again = st.save_preset("soft", "Basics > Blur", {"0": 3})        # same filter + name (any case): replaced
    assert again["id"] == a["id"] and again["values"] == {"0": 3} and again["seed"] is None and len(st) == 3
    assert st.rename(a["id"], "Gentle") and not st.rename(a["id"], "Strong")      # clash refused
    assert st.delete(a["id"]) and not st.delete(a["id"]) and len(st) == 2
    with pytest.raises(ValueError):
        st.save_preset("  ", "Basics > Blur", {})
    assert len(PresetStore(st.path)) == 2                              # persisted


def test_presets_migrate_the_old_format_and_keep_a_backup(tmp_path):
    old = tmp_path / "presets.json"
    old.write_text(json.dumps({"Basics > Blur": {"Soft": {"0": 2}, "Hard": {"0": 30}}, "Basics > Sepia": {"x": {}}}))
    st = PresetStore(str(old))
    assert sorted((p["filter"], p["name"]) for p in st.all()) == [("Basics > Blur", "Hard"), ("Basics > Blur", "Soft"),
                                                                  ("Basics > Sepia", "x")]
    assert (tmp_path / "presets.json.v1.bak").exists()
    assert json.loads(old.read_text())["version"] == 2


def test_presets_ignore_garbage(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"version": 2, "presets": [{"name": "ok", "filter": "A", "values": {}}, {"name": 3}, "x",
                                                        {"name": "n", "filter": "A", "values": {}, "seed": True}]}))
    st = PresetStore(str(f))
    assert [p["name"] for p in st.all()] == ["n", "ok"] and all(p["seed"] is None for p in st.all())
    f.write_text("{not json")
    assert len(PresetStore(str(f))) == 0


def test_presets_export_import_merge(tmp_path):
    a = PresetStore(str(tmp_path / "a.json"))
    a.save_preset("One", "F > X", {"0": 1}, seed=5)
    a.save_preset("Two", "F > Y", {"0": 2})
    share = tmp_path / "share.json"
    assert a.export_file(str(share)) == 2
    assert a.export_file(str(share), ids=[a.find("F > X", "One")["id"]]) == 1
    b = PresetStore(str(tmp_path / "b.json"))
    b.save_preset("One", "F > X", {"0": 99})
    added, replaced = b.import_file(str(share))
    assert (added, replaced) == (0, 1) and b.find("F > X", "One")["values"] == {"0": 1} and b.find("F > X", "One")["seed"] == 5
    a.export_file(str(share))
    assert b.import_file(str(share)) == (1, 1) and len(b) == 2
    (tmp_path / "other.json").write_text(json.dumps({"hello": 1}))
    with pytest.raises(ValueError):
        b.import_file(str(tmp_path / "other.json"))


def test_presets_file_location_is_configurable(tmp_path):
    s = Settings()
    assert s.presets_file().endswith("presets.json") and "home" in s.presets_file()
    s.set("presets_path", str(tmp_path / "synced" / "mine.json"))
    assert Settings().presets_file() == str(tmp_path / "synced" / "mine.json")
    st = PresetStore(Settings().presets_file())
    st.save_preset("x", "A", {})
    assert (tmp_path / "synced" / "mine.json").exists()                # folder is created on demand


# -- one shared preset listing --------------------------------------------------------
from gmic_studio.core import library


def _library_fixture(tmp_path):
    idx = filters.FilterIndex.load()
    st = PresetStore(str(tmp_path / "p.json"))
    for name, fid in (("Dreamy glow", "Basics > Blur"), ("Crisp", "Basics > Sharpen"), ("Old photo", "Basics > Sepia"),
                      ("Soft", "Basics > Blur"), ("Orphan", "Gone > Filter")):
        st.save_preset(name, fid, {})
    return idx, st


def test_list_presets_shows_everything_across_filters(tmp_path):
    idx, st = _library_fixture(tmp_path)
    names = [p["name"] for p in library.list_presets(st, idx)]
    assert sorted(names) == ["Crisp", "Dreamy glow", "Old photo", "Orphan", "Soft"]       # all filters, one list
    assert [p["name"] for p in library.list_presets(st, idx, only_filter="Basics > Blur")] == ["Dreamy glow", "Soft"]


def test_list_presets_orders_by_recent_use_then_name(tmp_path):
    idx, st = _library_fixture(tmp_path)
    assert [p["name"] for p in library.list_presets(st, idx)][:2] == ["Crisp", "Dreamy glow"]      # alphabetical
    st.touch(st.find("Basics > Sepia", "Old photo")["id"])
    st.find("Basics > Blur", "Soft")["used"] = "2000-01-01T00:00:00"
    st.save()
    assert [p["name"] for p in library.list_presets(st, idx)][:2] == ["Old photo", "Soft"]
    assert PresetStore(st.path).find("Basics > Sepia", "Old photo")["used"]                        # persisted


def test_list_presets_search_matches_name_or_filter_words(tmp_path):
    idx, st = _library_fixture(tmp_path)
    assert [p["name"] for p in library.list_presets(st, idx, "blur")] == ["Dreamy glow", "Soft"]     # by filter name
    assert [p["name"] for p in library.list_presets(st, idx, "soft blur")] == ["Soft"]               # all words must match
    assert [p["name"] for p in library.list_presets(st, idx, "cris")] == ["Crisp"]
    assert library.list_presets(st, idx, "zzz") == []
    assert [p["name"] for p in library.list_presets(st, idx, "so")][:1] == ["Soft"]                  # name match ranks first


def test_display_name(tmp_path):
    idx, st = _library_fixture(tmp_path)
    assert library.display_name(st.find("Basics > Blur", "Soft"), idx) == "Soft  ·  Blur"
    assert library.display_name(st.find("Gone > Filter", "Orphan"), idx) == "Orphan  ·  Filter"


# -- randomizer ---------------------------------------------------------------------------------
import random as _random

from gmic_studio.core import randomize as rnd

_RAND_DEF = '''#@gui T > R : cmd
#@gui : n = int(5,0,10)
#@gui : f = float(1.5,-2,2)
#@gui : b = bool(0)
#@gui : c = choice(1,"a","b","c")
#@gui : col = color(10,20,30,200)
#@gui : pt = point(50,50)
#@gui : t = text("keep me")
#@gui : note = note("n")
#@gui : btn = button(0)
'''


def test_randomize_stays_in_range_skips_text_and_notes_and_respects_locks():
    fdef = filters.parse_definitions(_RAND_DEF)[0]
    base = fdef.defaults()
    seen = {"n": set(), "b": set(), "c": set()}
    for seed in range(300):
        out = rnd.randomize(fdef, base, locked={"1"}, rng=_random.Random(seed))
        assert 0 <= out["0"] <= 10 and isinstance(out["0"], int)
        assert out["1"] == 1.5                                    # locked
        assert isinstance(out["2"], bool) and out["3"] in (0, 1, 2)
        assert all(0 <= v <= 255 for v in out["4"][:3]) and out["4"][3] == 200       # alpha untouched
        assert all(0 <= v <= 100 for v in out["5"])
        assert out["6"] == "keep me" and out["8"] == base["8"]    # text and buttons are never touched
        seen["n"].add(out["0"]); seen["b"].add(out["2"]); seen["c"].add(out["3"])
    assert seen["n"] == set(range(11)) and seen["b"] == {True, False} and seen["c"] == {0, 1, 2}


def test_randomize_is_deterministic_for_a_seeded_rng_and_can_nudge():
    fdef = filters.parse_definitions(_RAND_DEF)[0]
    base = fdef.defaults()
    assert rnd.randomize(fdef, base, rng=_random.Random(7)) == rnd.randomize(fdef, base, rng=_random.Random(7))
    far = near = 0
    for seed in range(200):
        out = rnd.randomize(fdef, base, rng=_random.Random(seed), strength=0.1)
        assert abs(out["0"] - 5) <= 1 and abs(out["1"] - 1.5) <= 0.4 + 1e-9          # +-10% of the range
        assert all(abs(a - b) <= 26 for a, b in zip(out["4"][:3], base["4"][:3]))
        far += abs(out["0"] - 5) > 0
    assert far > 0                                                                    # it does move
    assert rnd.randomize(filters.FilterDef((), "x", "c", []), {}) == {}


# -- batch delete and favorites in the stores ---------------------------------------------------
def test_batch_delete_presets_writes_once_and_counts(tmp_path):
    st = PresetStore(str(tmp_path / "p.json"))
    ids = [st.save_preset("p%d" % i, "F > X", {})["id"] for i in range(5)]
    assert st.delete_many(ids[:3] + ["nope"]) == 3 and len(st) == 2
    assert st.delete_many(ids[:3]) == 0 and len(PresetStore(st.path)) == 2


def test_remove_many_favorites_and_slow_filter_memory():
    s = Settings()
    for f in ("A", "B", "C", "D"):
        s.toggle_favorite(f)
    assert s.remove_favorites(["B", "D", "zzz"]) == 2 and Settings().get("favorites") == ["A", "C"]
    assert s.remove_favorites([]) == 0
    s.note_preview_time("A", 1.0)
    assert not s.is_slow("A")
    s.note_preview_time("A", 9.0)
    assert s.is_slow("A") and Settings().is_slow("A")
    s.note_preview_time("A", 0.5)                                    # it became fast again
    assert not s.is_slow("A")
    s.note_preview_time("A", 9.0)
    s.set("fast_slow_previews", False)
    assert not s.is_slow("A")                                        # the option switches the behaviour off


def test_list_favorites_only_lists_installed_filters_sorted():
    idx = filters.FilterIndex.load()
    got = library.list_favorites(idx, ["Basics > Sepia", "Gone > Missing", "Basics > Blur"])
    assert [f.name for f in got] == ["Blur", "Sepia"]
    assert [f.name for f in library.list_favorites(idx, ["Basics > Sepia", "Basics > Blur"], "sep")] == ["Sepia"]
    assert library.list_favorites(idx, []) == []


# -- preview commands -----------------------------------------------------------------------------
def test_preview_command_parsed_from_definitions():
    fs = filters.parse_definitions('''#@gui A > Brush : fx_brush, fx_brush_preview(0)
#@gui : x = float(1,0,2)
#@gui A > Same : fx_same, fx_same
#@gui A > Placeholder : fx_p, _none_
#@gui A > NoPreview : fx_n, gui_no_preview
#@gui A > Zoomed : fx_z, fx_z_preview(1)*
#@gui A > Bare : fx_bare
''')
    by = {f.name: f for f in fs}
    assert by["Brush"].preview_command == "fx_brush_preview" and by["Brush"].preview_factor == 0.0
    assert by["Same"].preview_command == "" and by["Placeholder"].preview_command == "" and by["NoPreview"].preview_command == ""
    assert by["Zoomed"].preview_command == "fx_z_preview" and by["Zoomed"].preview_factor == 1.0
    assert by["Bare"].preview_command == ""
    assert filters.useful_preview("c", "bad name; shell") == "" and filters.useful_preview("c", "-c_preview") == "c_preview"


def test_preview_command_read_from_the_krita_cache():
    data = _cache_bytes([("A", ["C"], "fx_a", "X=int(1,0,2)")])           # the builder writes "<cmd>_preview" as preview command
    fs = gmicqt_cache.parse(data)
    assert fs[0].command == "fx_a" and fs[0].preview_command == "fx_a_preview"


# -- presets stored in the document, next to the library ---------------------------------------------------------
from gmic_studio.core.presets import CallbackBackend, PresetBook


def _doc_backend():
    box = {"data": {}, "writes": 0}

    def read():
        return box["data"]

    def write(data):
        box["data"], box["writes"] = data, box["writes"] + 1

    return CallbackBackend(read, write), box


def test_a_store_can_live_anywhere_a_backend_puts_it():
    backend, box = _doc_backend()
    st = PresetStore(backend)
    p = st.save_preset("In doc", "F > X", {"0": 1}, seed=4)
    assert box["writes"] == 1 and box["data"]["version"] == 2 and box["data"]["presets"][0]["name"] == "In doc"
    again = PresetStore(CallbackBackend(lambda: box["data"], lambda d: None))          # "reopening the file"
    assert again.find("F > X", "In doc")["id"] == p["id"] and again.path == ""
    assert len(PresetStore(CallbackBackend(lambda: (_ for _ in ()).throw(RuntimeError("closed")), lambda d: None))) == 0


def test_book_keeps_library_and_document_apart_and_copies_on_request(tmp_path):
    backend, box = _doc_backend()
    book = PresetBook(PresetStore(str(tmp_path / "lib.json")))
    assert not book.has_document and len(book) == 0
    with pytest.raises(ValueError):
        book.save_preset("x", "F > X", {}, scope="document")                  # nowhere to put it without a document
    book.set_document(PresetStore(backend))
    lib = book.save_preset("Shared", "F > X", {"0": 1})
    doc = book.save_preset("Shared", "F > X", {"0": 2}, scope="document")     # same name in both: no clash
    assert (lib["scope"], doc["scope"]) == ("library", "document") and len(book) == 2
    assert [p["scope"] for p in book.all()] == ["library", "document"]
    assert PresetStore(str(tmp_path / "lib.json")).find("F > X", "Shared")["values"] == {"0": 1}      # library file untouched
    assert box["data"]["presets"][0]["values"] == {"0": 2}
    assert book.scope_of(lib["id"]) == "library" and book.scope_of(doc["id"]) == "document" and book.scope_of("nope") is None
    only_lib = book.save_preset("Only mine", "F > Y", {})
    copy = book.copy(only_lib["id"], "document")
    assert copy["scope"] == "document" and copy["id"] != only_lib["id"] and len(book) == 4
    assert book.copy(only_lib["id"], "library") is None                       # already there
    assert book.copy(copy["id"], "library")["scope"] == "library" and len(book) == 4 and book.find("F > Y", "Only mine")["id"] == only_lib["id"]
    assert book.rename(doc["id"], "Renamed") and book.get(doc["id"])["name"] == "Renamed" and book.get(lib["id"])["name"] == "Shared"
    book.touch(doc["id"])
    assert book.get(doc["id"])["used"] and not book.get(lib["id"])["used"]
    assert book.delete_many([lib["id"], doc["id"], "nope"]) == 2 and len(book) == 2


def test_book_without_a_document_behaves_like_the_plain_library(tmp_path):
    book = PresetBook(PresetStore(str(tmp_path / "lib.json")))
    p = book.save_preset("a", "F > X", {})
    assert book.copy(p["id"], "document") is None and book.find("F > X", "a", "document") is None
    assert book.delete(p["id"]) and not book.delete(p["id"])
    book.set_document(PresetStore(_doc_backend()[0]))
    book.set_document(None)                                                   # the document was closed
    assert not book.has_document and len(book) == 0


def test_book_export_includes_both_sources_and_import_goes_to_the_library(tmp_path):
    book = PresetBook(PresetStore(str(tmp_path / "lib.json")), PresetStore(_doc_backend()[0]))
    book.save_preset("L", "F > X", {})
    book.save_preset("D", "F > Y", {}, scope="document")
    share = tmp_path / "share.json"
    assert book.export_file(str(share)) == 2
    assert all("scope" not in p for p in json.loads(share.read_text())["presets"])
    other = PresetBook(PresetStore(str(tmp_path / "other.json")))
    assert other.import_file(str(share)) == (2, 0) and len(other) == 2
    with pytest.raises(ValueError):
        other.import_file(str(share), scope="document")


def test_list_presets_can_filter_by_source(tmp_path):
    idx = filters.FilterIndex.load()
    book = PresetBook(PresetStore(str(tmp_path / "lib.json")), PresetStore(_doc_backend()[0]))
    book.save_preset("A", "Basics > Blur", {})
    book.save_preset("B", "Basics > Blur", {}, scope="document")
    names = lambda scope: [p["name"] for p in library.list_presets(book, idx, scope=scope)]
    assert names(None) == ["A", "B"] and names("library") == ["A"] and names("document") == ["B"]


# -- point colors and ranges that always contain the default -----------------------------------------------------
def test_point_handle_color_is_read_from_the_definition():
    ps = filters.parse_param_block('A=point(80,20,0,1,255,128,0,255,5)\nB=point(10,90)\nC=point(50,50,-1,1,300,0,-5,255)')
    assert ps[0].color == [255, 128, 0] and ps[0].default == [80.0, 20.0]
    assert ps[1].color is None                                   # none given: the UI picks a visible default
    assert ps[2].color == [255, 0, 0]                            # clamped into 0..255


def test_a_default_outside_the_stated_range_is_pulled_into_it_like_gmic_qt_does():
    ps = filters.parse_param_block('S=float(3,0,2)\nN=int(0,1,500)\nOK=float(1,0,2)\nA=float(-180,0,180)\nR=int(5,10,0)')
    assert (ps[0].minimum, ps[0].maximum, ps[0].default) == (0.0, 2.0, 2.0)
    assert (ps[1].minimum, ps[1].maximum, ps[1].default) == (1, 500, 1)
    assert (ps[2].minimum, ps[2].maximum, ps[2].default) == (0.0, 2.0, 1.0)
    assert (ps[3].minimum, ps[3].maximum, ps[3].default) == (0.0, 180.0, 0.0)      # Quick Copyright's Orientation: not upside down
    assert (ps[4].minimum, ps[4].maximum, ps[4].default) == (0, 10, 5)             # a reversed range is put in order
    f = filters.FilterDef((), "x", "c", ps)
    assert f.command_line()[1][:2] == ["2", "1"]

def test_an_index_keeps_filters_whose_line_ends_with_an_input_mode():
    text = ("#@gui 3D Mesh:fx_mesh3d,fx_mesh3d_preview(1) : *\n#@gui :Size=float(1,0,2)\n"
            "#@gui Old Style : fx_old, fx_old_preview(1)\n#@gui :N=int(1,0,2)\n")
    fs = filters.parse_definitions(text)
    assert [(f.name, f.command, f.preview_command, len(f.params)) for f in fs] == [
        ("3D Mesh", "fx_mesh3d", "fx_mesh3d_preview", 1), ("Old Style", "fx_old", "fx_old_preview", 1)]


def test_status_groups_are_split_and_a_real_filter_can_report_values(tmp_path):
    assert engine.split_groups("{1}{2,3}{}{a b}") == ["1", "2,3", "", "a b"]
    assert engine.split_groups("") == [] and engine.split_groups("x{a{b}c}") == ["a{b}c"]
    exe = engine.locate()
    if not exe:
        pytest.skip("needs a real gmic")
    defs = tmp_path / "d.gmic"
    defs.write_text("studio_report : u \\{\\}\\{9,8\\}\\{$1\\}\n")
    job = engine.Job("studio_report", ["4"], 2, 2, bytes([9, 9, 9, 255]) * 4, definitions=str(defs))
    w, h, depth, path, workdir = engine.Runner(exe, job, timeout=60).run()
    try:
        assert engine.split_groups(engine.read_status(workdir)) == ["", "9,8", "4"]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_folder_lines_give_the_filters_their_categories_and_info_entries_are_skipped():
    text = ("#@gui _<b>Rendering</b>\n#@gui A:fx_a\n#@gui _<b>Silhouettes</b>\n#@gui <b>Animals</b>\n#@gui Cat:fx_cat\n"
            "#@gui _<b>Icons</b>\n#@gui Star:fx_star\n#@gui _\n#@gui Loose:fx_loose\n#@gui __<b>Testing</b>\n"
            "#@gui <i>Afre</i>\n#@gui B > C:fx_c\n#@gui !<b><i>[ New Version Available! ]</i></b> : _none_, _none_\n")
    assert [f.id for f in filters.parse_definitions(text)] == [
        "Rendering > A", "Silhouettes > Animals > Cat", "Silhouettes > Icons > Star", "Silhouettes > Loose",
        "Testing > Afre > B > C"]


def test_split_override_takes_gmics_definition_and_moves_its_position(tmp_path, monkeypatch):
    upd = tmp_path / "update999.gmic"
    upd.write_text("other :\nfoo\ngui_split_preview : check 1 skip 2\nis_movable:=!isnan($3)\n"
                   + engine._SPLIT_POS + "\nline 0,$posy\ngui_print_preview : bar\n")
    monkeypatch.setattr(filters, "find_definition_files", lambda explicit="": [str(upd)])
    engine._split_cache.clear()
    text = engine.split_override((30, 70))
    lines = text.splitlines()
    assert lines[0].startswith("gui_split_preview :") and "gui_print_preview" not in text and "other" not in text
    assert "posx,posy:=$is_movable?cut([30,70],0,100):[50,50]" in lines and "line 0,$posy" in lines
    assert "if $is_movable _studio_split_mode=$2 fi" in lines                    # it reports itself
    assert engine._SPLIT_POS in engine.split_override(None)                      # watching only: position untouched
    upd.write_text("nothing here\n")
    engine._split_cache.clear()
    assert engine.split_override((1, 2)) == ""                                   # a changed definition: fall back quietly
    script = engine.build_script(job(watch_split=True))
    assert script.rstrip().endswith("('M:${_studio_split_mode}') output[-1] split.txt")
