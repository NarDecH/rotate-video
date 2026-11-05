"""Resize the app to a short window and verify that wheel scrolling really works.

Uses real cursor + wheel input (PostMessage wheel events are ignored by Tk on
Windows because it hit-tests with the physical cursor position).
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageGrab

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
MOUSEEVENTF_WHEEL = 0x0800


def find_window(needle: str, timeout: float = 60.0):
    deadline = time.time() + timeout

    def search():
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

    while time.time() < deadline:
        hwnd = search()
        if hwnd:
            return hwnd
        time.sleep(0.4)
    return None


def shoot(hwnd, out: Path) -> None:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom),
                   all_screens=True).save(out)


def main() -> int:
    exe = Path(sys.argv[1]).resolve()
    before = Path(sys.argv[2]).resolve()
    after = Path(sys.argv[3]).resolve()

    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent))
    hwnd = find_window("Rotate Video")
    if not hwnd:
        print("window not found")
        proc.terminate()
        return 1

    user32.SetWindowPos(hwnd, 0, 100, 60, 800, 690, 0x0004 | 0x0010)
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.6)
    shoot(hwnd, before)

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    old = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    user32.SetCursorPos(rect.left + 300, rect.top + 400)
    time.sleep(0.3)

    for _ in range(6):
        user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, ctypes.c_uint32(-120 & 0xFFFFFFFF), 0)
        time.sleep(0.08)
    time.sleep(0.9)

    shoot(hwnd, after)
    user32.SetCursorPos(old.x, old.y)

    changed = Image.open(before).tobytes() != Image.open(after).tobytes()
    print(f"scrolled: {changed}")

    user32.PostMessageW(hwnd, 0x0010, 0, 0)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.terminate()
    return 0 if changed else 2


if __name__ == "__main__":
    raise SystemExit(main())
