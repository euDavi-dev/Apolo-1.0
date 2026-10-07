"""Iniciar com o Windows (chave HKCU\\...\\Run — não exige administrador)."""
import sys
from pathlib import Path

from app.core.config import ROOT

KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "APOLO"


def _command() -> str:
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        return f'"{exe}" --minimized'
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" "{ROOT / "run.py"}" --minimized'


def is_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
            winreg.QueryValueEx(k, NAME)
            return True
    except Exception:
        return False


def set_enabled(enabled: bool) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(k, NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False
