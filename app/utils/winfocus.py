import ctypes
import sys


def force_foreground(hwnd: int) -> None:
    """O Windows impede janelas de roubar o foco; o truque da tecla ALT libera o SetForegroundWindow."""
    if sys.platform != "win32":
        return
    try:
        u = ctypes.windll.user32
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)  # SW_RESTORE
        u.keybd_event(0x12, 0, 0, 0)
        u.SetForegroundWindow(hwnd)
        u.keybd_event(0x12, 0, 2, 0)
    except Exception:
        pass
