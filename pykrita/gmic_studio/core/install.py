"""Import a gmic CLI the user downloaded (zip / tar archive) into the plugin's private folder."""
import os
import shutil
import tarfile
import zipfile

from . import paths

MAX_BYTES = 2 * 1024 ** 3


class InstallError(Exception):
    pass


def _inside(root, target):
    root = os.path.realpath(root)
    return os.path.realpath(target).startswith(root + os.sep)


def _extract_zip(archive, dest):
    with zipfile.ZipFile(archive) as zf:
        if sum(i.file_size for i in zf.infolist()) > MAX_BYTES:
            raise InstallError("archive is unreasonably large")
        for info in zf.infolist():
            if not _inside(dest, os.path.join(dest, info.filename)):
                raise InstallError("unsafe path in archive: %s" % info.filename)
        zf.extractall(dest)


def _extract_tar(archive, dest):
    with tarfile.open(archive) as tf:
        members = tf.getmembers()
        if sum(m.size for m in members) > MAX_BYTES:
            raise InstallError("archive is unreasonably large")
        for m in members:
            if not _inside(dest, os.path.join(dest, m.name)) or m.issym() or m.islnk() or m.isdev():
                raise InstallError("unsafe entry in archive: %s" % m.name)
        tf.extractall(dest)


def install_archive(archive):
    """Extract `archive` and return the path of the gmic executable inside it."""
    name = paths.exe_name()
    dest = os.path.join(paths.private_bin_dir(), "gmic-import")
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    try:
        if zipfile.is_zipfile(archive):
            _extract_zip(archive, dest)
        elif tarfile.is_tarfile(archive):
            _extract_tar(archive, dest)
        else:
            raise InstallError("not a zip or tar archive")
        for root, _dirs, files in os.walk(dest):
            for f in files:
                if f.lower() == name:
                    exe = os.path.join(root, f)
                    os.chmod(exe, os.stat(exe).st_mode | 0o755)
                    return exe
        raise InstallError("no %s found inside the archive" % name)
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        raise InstallError(str(exc))
    except InstallError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
