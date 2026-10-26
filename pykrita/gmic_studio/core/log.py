"""A small rotating log file in the plugin's config folder, so problems can be diagnosed afterwards."""
import logging
import logging.handlers
import os
import threading

from . import paths

_NAME = "gmic_studio"
_lock = threading.Lock()
_handler = None
_handler_path = None


def path():
    return os.path.join(paths.config_dir(), "gmic_studio.log")


def get():
    """The plugin's logger, writing to the file in the current config folder (safe to call from any thread)."""
    global _handler, _handler_path
    logger = logging.getLogger(_NAME)
    wanted = path()
    with _lock:
        if _handler is None or _handler_path != wanted:
            _drop(logger)
            try:
                _handler = logging.handlers.RotatingFileHandler(wanted, maxBytes=200000, backupCount=1, encoding="utf-8")
            except OSError:
                _handler = logging.NullHandler()
            _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            _handler_path = wanted
            logger.addHandler(_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def _drop(logger):
    global _handler
    if _handler is not None:
        logger.removeHandler(_handler)
        _handler.close()
        _handler = None


def reset():
    """Close the log file (it is reopened on the next `get()`)."""
    with _lock:
        _drop(logging.getLogger(_NAME))
