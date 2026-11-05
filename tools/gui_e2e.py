"""End-to-end GUI test for the packaged exe.

Usage: gui_e2e.py <exe> <screenshot.png> <video> [video ...]

Launches the app with the files, presses Enter to start the queue, waits for all
outputs, verifies they exist and screenshots the finished window.
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
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


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


def screenshot(hwnd, out: Path) -> None:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom),
                         all_screens=True)
    img.save(out)
    print(f"screenshot {img.size[0]}x{img.size[1]} -> {out}")


def main() -> int:
    exe = Path(sys.argv[1]).resolve()
    shot = Path(sys.argv[2]).resolve()
    videos = [Path(a).resolve() for a in sys.argv[3:]]
    expected = [v.with_name(v.stem + "_rot90cw" + v.suffix) for v in videos]

    for path in expected:
        path.unlink(missing_ok=True)

    proc = subprocess.Popen([str(exe)] + [str(v) for v in videos], cwd=str(exe.parent))
    hwnd = find_window("Rotate Video")
    if not hwnd:
        print("FAIL: window did not open")
        proc.terminate()
        return 1
    print("PASS: window opened")

    user32.SetForegroundWindow(hwnd)
    time.sleep(2.0)  # let the probes finish

    user32.keybd_event(0x0D, 0, 0, 0)  # Enter
    user32.keybd_event(0x0D, 0, 2, 0)

    deadline = time.time() + 120
    while time.time() < deadline:
        if all(p.exists() and p.stat().st_size > 0 for p in expected):
            break
        time.sleep(0.5)

    ok = all(p.exists() and p.stat().st_size > 0 for p in expected)
    for p in expected:
        print(("PASS" if p.exists() else "FAIL") + f": {p.name}")
    if ok:
        time.sleep(2.0)
        screenshot(hwnd, shot)

    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.terminate()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
