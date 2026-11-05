"""Launch the built exe, screenshot its window, then close it.

Used only for verification during development.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def find_window(needle: str):
    hits: list[int] = []

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if needle in buf.value:
            hits.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return hits[0] if hits else None


def main() -> int:
    exe = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()

    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent))
    hwnd = None
    for _ in range(120):
        hwnd = find_window("Rotate Video")
        if hwnd:
            break
        time.sleep(0.5)
    if not hwnd:
        print("window not found")
        proc.terminate()
        return 1

    user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE then restore to force foreground
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.2)

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    bbox = (rect.left, rect.top, rect.right, rect.bottom)
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    img.save(out)
    print(f"captured {img.size[0]}x{img.size[1]} -> {out}")

    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
