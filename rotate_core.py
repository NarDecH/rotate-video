"""rotate_core.py - FFmpeg-powered video rotation engine.

Modes:
  copy     - only writes the display-matrix (rotation) metadata, no re-encode
  crf      - bakes the rotation into the pixels with high quality H.264
  lossless - bakes the rotation with mathematically lossless H.264

The FFmpeg binary is bundled with the app; nothing has to be installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

MIN_BINARY_BYTES = 1_000_000
CREATE_NO_WINDOW = 0x08000000

KNOWN_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".webm",
    ".avi", ".wmv", ".flv", ".ts", ".mts", ".m2ts", ".ogv", ".3gp",
}
# Containers whose muxer can store a display matrix (rotation) reliably.
COPY_SAFE_CONTAINERS = {".mp4", ".m4v", ".mov", ".mkv", ".webm"}
# Containers that cannot hold H.264/AAC properly -> write .mp4 instead.
FORCE_MP4_CONTAINERS = {".ogv", ".wmv", ".avi"}
# Audio codecs that may be stream-copied into MP4.
MP4_SAFE_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}

_ALLOWED_PIX_FMTS = {
    "yuv420p", "yuv422p", "yuv444p",
    "yuv420p10le", "yuv422p10le", "yuv444p10le",
    "yuv420p12le", "nv12",
}


# --------------------------------------------------------------------------- #
# Actions / modes
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Action:
    key: str
    label: str
    suffix: str
    delta_ccw: float          # rotation to add, in counter-clockwise degrees
    video_filter: str         # filter used when pixels are re-encoded
    is_flip: bool = False


ACTIONS: dict[str, Action] = {
    "cw90":   Action("cw90",   "หมุนขวา 90°",     "_rot90cw",  -90.0, "transpose=1"),
    "ccw90":  Action("ccw90",  "หมุนซ้าย 90°",    "_rot90ccw",  90.0, "transpose=2"),
    "rot180": Action("rot180", "หมุน 180°",        "_rot180",   180.0, "hflip,vflip"),
    "fliph":  Action("fliph",  "พลิกซ้าย–ขวา",    "_fliph",      0.0, "hflip", True),
    "flipv":  Action("flipv",  "พลิกบน–ล่าง",     "_flipv",      0.0, "vflip", True),
}

MODES: dict[str, str] = {
    "copy": "ไม่เข้ารหัสใหม่ (คุณภาพ 100% เดิม)",
    "crf": "ฝังการหมุนจริง — คุณภาพสูง (CRF 16)",
    "lossless": "ฝังการหมุนจริง — Lossless (pixel-exact)",
}


# --------------------------------------------------------------------------- #
# FFmpeg discovery (bundled -> cache -> PATH -> imageio-ffmpeg)
# --------------------------------------------------------------------------- #

def cache_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "RotateVideo"
    return Path.home() / ".local" / "share" / "rotate-video"


def _bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


_ffmpeg_cache: Optional[Path] = None


def ffmpeg_exe() -> Path:
    """Return a usable ffmpeg executable, extracting the bundled copy once."""
    global _ffmpeg_cache
    if _ffmpeg_cache is not None and _ffmpeg_cache.exists():
        return _ffmpeg_cache

    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    cached = cache_dir() / "bin" / name

    if cached.exists() and cached.stat().st_size > MIN_BINARY_BYTES:
        _ffmpeg_cache = cached
        return cached

    here = Path(__file__).resolve().parent
    for src in (_bundle_dir() / name, _bundle_dir() / "vendor" / name, here / "vendor" / name):
        if src.exists() and src.stat().st_size > MIN_BINARY_BYTES:
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                part = cached.with_name(cached.name + ".part")
                shutil.copyfile(src, part)
                os.replace(part, cached)
                _ffmpeg_cache = cached
            except OSError:
                _ffmpeg_cache = src  # read-only profile: run it in place
            return _ffmpeg_cache

    found = shutil.which("ffmpeg")
    if found:
        _ffmpeg_cache = Path(found)
        return _ffmpeg_cache

    try:  # development convenience only
        import imageio_ffmpeg  # type: ignore

        _ffmpeg_cache = Path(imageio_ffmpeg.get_ffmpeg_exe())
        return _ffmpeg_cache
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "ไม่พบ FFmpeg ในตัวโปรแกรม (ไฟล์อาจเสียหาย) — กรุณาดาวน์โหลดโปรแกรมใหม่"
        ) from exc


def _popen_kwargs() -> dict:
    return {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}


def attach_console() -> None:
    """Make print() work when a windowed exe is started from a console."""
    if os.name != "nt":
        return
    try:
        if sys.stdout is not None and sys.stdout.isatty():
            return
    except Exception:
        pass
    try:
        ctypes = __import__("ctypes")
        ctypes.windll.kernel32.AttachConsole(-1)  # ATTACH_PARENT_PROCESS
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stderr = sys.stdout
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Encoder capabilities
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def available_encoders() -> frozenset[str]:
    try:
        out = subprocess.run(
            [str(ffmpeg_exe()), "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            **_popen_kwargs())
    except Exception:
        return frozenset()
    return frozenset(re.findall(r"^\s*[VAS][A-Z.]{5}\s+(\S+)", out.stdout or "", re.M))


@lru_cache(maxsize=1)
def gpu_encoder() -> Optional[str]:
    if os.name != "nt":
        return None
    encoders = available_encoders()
    for name in ("h264_nvenc", "h264_qsv", "h264_amf"):
        if name in encoders:
            return name
    return None


@lru_cache(maxsize=1)
def gpu_works() -> bool:
    """Really try a one-frame encode, so we never promise a GPU we cannot use."""
    enc = gpu_encoder()
    if not enc:
        return False
    try:
        proc = subprocess.run(
            [str(ffmpeg_exe()), "-v", "error", "-f", "lavfi",
             "-i", "color=c=black:s=128x128:d=0.1", "-frames:v", "1",
             "-c:v", enc, "-f", "null", "-"],
            capture_output=True, timeout=25, **_popen_kwargs())
        return proc.returncode == 0
    except Exception:
        return False


def _gpu_args(enc: str) -> list[str]:
    if enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", "19", "-b:v", "0"]
    if enc == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "19"]
    return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
            "-qp_i", "20", "-qp_p", "20"]


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?: Video: (.*)$")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: (.*)$")
_SUB_RE = re.compile(r"Stream #\d+:\d+.*?: Subtitle:")
_ATTACH_RE = re.compile(r"Stream #\d+:\d+.*?: Attachment:")
_DIM_RE = re.compile(r"(\d{2,5})x(\d{2,5})")
_ROT_RE = re.compile(r"displaymatrix: rotation of (-?\d+(?:\.\d+)?) degrees")
_BITRATE_RE = re.compile(r"(\d+)\s*kb/s")
_FPS_RE = re.compile(r"([\d.]+)\s+fps")


@dataclass
class MediaInfo:
    width: int = 0
    height: int = 0
    duration: float = 0.0
    rotation: float = 0.0     # counter-clockwise degrees, as displayed
    has_audio: bool = False
    has_subs: bool = False
    has_attachments: bool = False
    pix_fmt: str = ""
    vcodec: str = ""
    acodec: str = ""
    video_bitrate: int = 0    # kb/s
    fps: float = 0.0
    is_hdr: bool = False

    @property
    def display_size(self) -> tuple[int, int]:
        if abs(self.rotation) % 180 == 90:
            return self.height, self.width
        return self.width, self.height


def _read_media_text(path: Path) -> str:
    proc = subprocess.run(
        [str(ffmpeg_exe()), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **_popen_kwargs())
    return (proc.stderr or "") + (proc.stdout or "")


def probe(path: Path) -> MediaInfo:
    """Read size, duration, audio, rotation and HDR flags with ffmpeg -i."""
    path = Path(path)
    text = _read_media_text(path)
    info = MediaInfo()

    m = _DURATION_RE.search(text)
    if m:
        info.duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    video_line = ""
    for line in text.splitlines():
        if _VIDEO_RE.search(line) and "(attached pic)" not in line:
            video_line = line
            break
    if not video_line:
        raise ValueError("ไฟล์นี้ไม่ใช่วิดีโอ หรือ FFmpeg อ่านไม่ได้")

    for dim in _DIM_RE.finditer(video_line):
        w, h = int(dim.group(1)), int(dim.group(2))
        if w >= 16 and h >= 16:
            info.width, info.height = w, h
            break

    codec = re.search(r"Video: ([A-Za-z0-9_]+)", video_line)
    if codec:
        info.vcodec = codec.group(1)

    pix = re.search(r"Video: [^,]*, ([A-Za-z0-9_]+)", video_line)
    if pix:
        info.pix_fmt = pix.group(1)

    bitrate = _BITRATE_RE.search(video_line)
    if bitrate:
        info.video_bitrate = int(bitrate.group(1))

    fps = _FPS_RE.search(video_line)
    if fps:
        try:
            info.fps = float(fps.group(1))
        except ValueError:
            pass

    info.is_hdr = ("smpte2084" in video_line) or ("arib-std-b67" in video_line)

    rot = _ROT_RE.search(text)
    if rot:
        info.rotation = float(rot.group(1))

    lines = text.splitlines()
    info.has_audio = any(_AUDIO_RE.search(line) for line in lines)
    info.has_subs = any(_SUB_RE.search(line) for line in lines)
    info.has_attachments = any(_ATTACH_RE.search(line) for line in lines)

    for line in lines:
        m2 = _AUDIO_RE.search(line)
        if m2:
            codec_name = re.search(r"Audio: ([A-Za-z0-9_]+)", line)
            info.acodec = codec_name.group(1) if codec_name else ""
            break
    return info


# --------------------------------------------------------------------------- #
# Planning helpers
# --------------------------------------------------------------------------- #

def supports_copy_mode(path: Path) -> bool:
    return Path(path).suffix.lower() in COPY_SAFE_CONTAINERS


def normalize_ccw(angle: float) -> float:
    """Normalize to (-180, 180] so 270 becomes -90."""
    a = angle % 360.0
    if a > 180.0:
        a -= 360.0
    if abs(a) < 0.01:
        return 0.0
    return a


def target_pix_fmt(info: MediaInfo) -> str:
    if info.pix_fmt in _ALLOWED_PIX_FMTS:
        return "yuv420p" if info.pix_fmt == "nv12" else info.pix_fmt
    return "yuv420p"


def output_extension(src_ext: str, mode: str) -> tuple[str, str]:
    """Pick the output container; returns (extension, note)."""
    src_ext = src_ext.lower()
    if mode == "copy":
        return src_ext, ""
    if src_ext in FORCE_MP4_CONTAINERS:
        return ".mp4", (f"บันทึกเป็น .mp4 เพราะคอนเทนเนอร์ {src_ext} "
                        "เก็บ H.264 ได้ไม่ดีหรือไม่รองรับ")
    return src_ext, ""


def expected_display_size(info: MediaInfo, action: Action) -> tuple[int, int]:
    dw, dh = info.display_size
    if action.is_flip:
        return dw, dh
    total = normalize_ccw(info.rotation + action.delta_ccw)
    return (dh, dw) if abs(total) % 180 == 90 else (dw, dh)


def estimate_output_bytes(info: MediaInfo, mode: str, src_size: int) -> int:
    """Rough output size estimate, deliberately on the safe (large) side."""
    if mode == "copy":
        return int(src_size * 1.01) + 200_000
    factor = 8.0 if mode == "lossless" else 1.15
    if info.video_bitrate and info.duration:
        base = info.video_bitrate * 1000 / 8 * info.duration
    else:
        base = float(src_size)
    return max(5_000_000, int(base * factor * 1.1))


def free_space(folder: Path) -> int:
    try:
        return shutil.disk_usage(str(folder)).free
    except OSError:
        return 0


def make_output_path(src: Path, action_key: str, out_dir: Optional[Path] = None,
                     ext: Optional[str] = None) -> Path:
    src = Path(src)
    folder = Path(out_dir) if out_dir else src.parent
    stem = src.stem + ACTIONS[action_key].suffix
    target_ext = ext or src.suffix or ".mp4"
    candidate = folder / f"{stem}{target_ext}"
    if candidate.resolve() == src.resolve():
        candidate = folder / f"{stem}_out{target_ext}"
    counter = 2
    while candidate.exists():
        candidate = folder / f"{stem}_{counter}{target_ext}"
        counter += 1
    return candidate


@dataclass
class Plan:
    src: Path
    dst: Path
    needed: int
    note: str = ""


def plan_files(files: list[Path], action_key: str, mode: str,
               out_dir: Optional[Path] = None) -> list[Plan]:
    plans: list[Plan] = []
    for src in files:
        info = probe(src)
        ext, note = output_extension(src.suffix, mode)
        dst = make_output_path(src, action_key, out_dir, ext)
        plans.append(Plan(src, dst, estimate_output_bytes(info, mode, src.stat().st_size), note))
    return plans


# --------------------------------------------------------------------------- #
# Command builder
# --------------------------------------------------------------------------- #

@dataclass
class CommandSpec:
    args: list[str]
    notes: list[str]
    used_gpu: bool = False


def build_command(src: Path, dst: Path, action_key: str, mode: str,
                  info: MediaInfo, use_gpu: bool = False) -> CommandSpec:
    action = ACTIONS[action_key]
    ff = str(ffmpeg_exe())
    ext = dst.suffix.lower()
    notes: list[str] = []

    if mode == "copy":
        if action.is_flip:
            raise ValueError("การพลิกภาพต้องเข้ารหัสใหม่ ไม่สามารถใช้โหมดคัดลอกได้")
        new_rotation = normalize_ccw(info.rotation + action.delta_ccw)
        args = [
            ff, "-hide_banner", "-y", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats",
            "-display_rotation", f"{new_rotation:g}",
            "-i", str(src),
            "-map", "0",
            "-c", "copy",
            "-map_metadata", "0",
            "-map_chapters", "0",
            str(dst),
        ]
        return CommandSpec(args, notes)

    args = [
        ff, "-hide_banner", "-y", "-loglevel", "error",
        "-progress", "pipe:1", "-nostats",
        "-i", str(src),
        # capital V skips cover-art streams ("attached pic")
        "-map", "0:V:0", "-map", "0:a?", "-map", "0:s?",
    ]
    if info.has_attachments and ext in {".mkv", ".mka"}:
        args += ["-map", "0:t?", "-c:t", "copy"]
        notes.append("คัดลอกไฟล์แนบ (เช่น ฟอนต์ซับ) ไปด้วย")

    args += ["-vf", action.video_filter]

    pix = target_pix_fmt(info)
    spec_gpu = False
    if use_gpu and mode != "lossless" and pix == "yuv420p":
        enc = gpu_encoder()
        if enc and gpu_works():
            args += _gpu_args(enc)
            spec_gpu = True
            notes.append(f"ใช้ GPU ({enc})")
        else:
            notes.append("ไม่พบ GPU ที่ใช้งานได้ — ใช้ CPU แทน")

    if not spec_gpu:
        if mode == "lossless":
            args += ["-c:v", "libx264", "-qp", "0", "-preset", "slow"]
        else:
            args += ["-c:v", "libx264", "-crf", "16", "-preset", "slow"]

    args += ["-pix_fmt", pix]

    audio_ok = (not info.has_audio) or info.acodec in MP4_SAFE_AUDIO
    if ext in {".mp4", ".m4v", ".mov"} and not audio_ok:
        args += ["-c:a", "aac", "-b:a", "192k"]
        notes.append("แปลงเสียงเป็น AAC เพราะปลายทางเป็น MP4")
    else:
        args += ["-c:a", "copy"]

    args += ["-c:s", "copy", "-map_metadata", "0", "-map_chapters", "0"]
    if ext in {".mp4", ".m4v", ".mov"}:
        args += ["-movflags", "+faststart"]
    args.append(str(dst))
    return CommandSpec(args, notes, spec_gpu)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

@dataclass
class RunResult:
    ok: bool
    returncode: int
    output: Path
    log: str = ""
    warning: str = ""
    verified: str = ""
    estimate: int = 0
    elapsed: float = 0.0


def _execute(spec: CommandSpec, info: MediaInfo,
             on_progress: Optional[Callable[[float, float], None]],
             cancel: Optional[object]) -> tuple[bool, int, bool, str]:
    log_lines: list[str] = []
    cancelled = False
    speed = 0.0
    duration = info.duration or 0.0

    proc = subprocess.Popen(
        spec.args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", **_popen_kwargs())

    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                cancelled = True
                proc.terminate()
                break
            if line.startswith("out_time_us="):
                if duration > 0 and on_progress:
                    try:
                        micros = int(line.split("=", 1)[1])
                        on_progress(min(99.0, micros / 1e6 / duration * 100.0), speed)
                    except ValueError:
                        pass
                continue
            if line.startswith("speed="):
                try:
                    speed = float(line.split("=", 1)[1].rstrip("x") or 0)
                except ValueError:
                    speed = 0.0
                continue
            if line.startswith(("out_time_ms=", "out_time=", "frame=", "fps=",
                                "progress=", "bitrate=", "total_size=",
                                "stream_0_", "dup_frames=", "drop_frames=")):
                continue
            if line:
                log_lines.append(line)
                if len(log_lines) > 40:
                    log_lines.pop(0)
    finally:
        proc.wait()

    return (proc.returncode == 0, proc.returncode, cancelled, "\n".join(log_lines))


def rotate(src: Path, action_key: str, mode: str, out_dir: Optional[Path] = None,
           on_progress: Optional[Callable[[float, float], None]] = None,
           cancel: Optional[object] = None, use_gpu: bool = False) -> RunResult:
    src = Path(src)
    if not src.exists():
        return RunResult(False, -1, src, f"ไม่พบไฟล์: {src}")

    try:
        info = probe(src)
    except Exception as exc:
        return RunResult(False, -1, src, str(exc))

    ext, note = output_extension(src.suffix, mode)
    dst = make_output_path(src, action_key, out_dir, ext)
    needed = estimate_output_bytes(info, mode, src.stat().st_size)
    free = free_space(dst.parent)
    if free and needed > free:
        return RunResult(False, -1, dst,
                         f"พื้นที่ว่างไม่พอ: ต้องใช้ประมาณ {human_size(needed)} "
                         f"แต่เหลือ {human_size(free)}", estimate=needed)

    started = time.monotonic()
    spec = build_command(src, dst, action_key, mode, info, use_gpu=use_gpu)
    ok, rc, cancelled, log = _execute(spec, info, on_progress, cancel)
    warning = ""

    if not ok and spec.used_gpu and not cancelled:
        retry = build_command(src, dst, action_key, mode, info, use_gpu=False)
        ok, rc, cancelled, log2 = _execute(retry, info, on_progress, cancel)
        if ok:
            warning = "GPU ใช้งานไม่ได้จริง — กลับไปใช้ CPU ให้แล้ว"
            log = log2

    if cancelled:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        return RunResult(False, rc or -1, dst, "ยกเลิกแล้ว", "", "", needed)

    if not ok or not dst.exists():
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        return RunResult(False, rc, dst, log or "FFmpeg ทำงานไม่สำเร็จ", "", "", needed)

    if on_progress:
        on_progress(100.0, 0.0)

    verified = ""
    try:
        out_info = probe(dst)
        if mode == "copy":
            want_rot = normalize_ccw(info.rotation + ACTIONS[action_key].delta_ccw)
            verified = f"{info.width}×{info.height} • หมุน {want_rot:g}°"
            if abs(normalize_ccw(out_info.rotation - want_rot)) > 1.0:
                warning = ("คอนเทนเนอร์นี้ไม่รองรับการเก็บค่าหมุน — "
                           "ควรใช้โหมด “ฝังการหมุนจริง” แทน")
        else:
            want_w, want_h = expected_display_size(info, ACTIONS[action_key])
            verified = f"{out_info.width}×{out_info.height} • หมุน 0°"
            if (out_info.width, out_info.height) != (want_w, want_h):
                warning = (f"ขนาดผลลัพธ์ไม่ตรงที่คาด "
                           f"({out_info.width}×{out_info.height} vs {want_w}×{want_h})")
            elif abs(out_info.rotation) > 1.0:
                warning = "ผลลัพธ์ยังมีค่าหมุนค้างอยู่ — อาจถูกหมุนซ้อน"
    except Exception:
        warning = warning or "ตรวจสอบไฟล์ผลลัพธ์ไม่ได้"

    if note:
        warning = (warning + " • " if warning else "") + note

    elapsed = time.monotonic() - started
    tail = "\n".join(x for x in (log, f"เสร็จใน {elapsed:.1f} วินาที") if x)
    return RunResult(True, 0, dst, tail, warning, verified, needed, elapsed)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def settings_path() -> Path:
    return cache_dir() / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".tmp")
        part.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(part, path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

def human_size(num_bytes: float) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def human_duration(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
