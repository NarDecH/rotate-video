#!/usr/bin/env bash
# Build RotateVideo (FFmpeg embedded).
#   bash build.sh              -> dist/RotateVideo.exe   (single file)
#   bash build.sh onedir       -> dist/RotateVideo/      (folder + zip, starts faster)
set -euo pipefail
cd "$(dirname "$0")"

PY=${PY:-python}
MODE=${1:-onefile}

echo "[1/5] เตรียม ffmpeg สำหรับฝังใน exe"
mkdir -p vendor
if [ ! -f vendor/ffmpeg.exe ]; then
  SRC=$("$PY" -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())")
  cp "$SRC" vendor/ffmpeg.exe
fi
ls -lh vendor/ffmpeg.exe

echo "[2/5] สร้างไอคอน"
"$PY" tools/make_icon.py

echo "[3/5] ตรวจสอบด้วย self-test (โหมด dev)"
"$PY" app.py --selftest

echo "[4/5] แพ็ก ($MODE)"
COMMON=(--noconfirm --clean --windowed --name RotateVideo
        --icon assets/app.ico --version-file version_info.txt
        --add-binary "vendor/ffmpeg.exe;."
        --add-data "assets/app.ico;assets"
        --exclude-module imageio_ffmpeg --exclude-module imageio
        --exclude-module numpy --exclude-module PIL --exclude-module cv2
        --exclude-module pytest --exclude-module matplotlib
        app.py)

if [ "$MODE" = "onedir" ]; then
  "$PY" -m PyInstaller "${COMMON[@]}" --onedir
  echo "[5/5] บีบอัดเป็น zip"
  "$PY" - <<'PY'
import shutil
path = shutil.make_archive("dist/RotateVideo-portable", "zip", "dist", "RotateVideo")
print("zip:", path)
PY
  du -sh dist/RotateVideo dist/RotateVideo-portable.zip
else
  "$PY" -m PyInstaller "${COMMON[@]}" --onefile
  ls -lh dist/RotateVideo.exe
  echo "เสร็จแล้ว: dist/RotateVideo.exe"
fi
