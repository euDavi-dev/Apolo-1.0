import logging
import sys
from logging.handlers import RotatingFileHandler

from app.core.config import DATA_DIR


def setup_logging(level=logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    if sys.stderr:  # pythonw.exe não tem stderr
        h = logging.StreamHandler()
        h.setFormatter(fmt)
        root.addHandler(h)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(DATA_DIR / "apolo.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
