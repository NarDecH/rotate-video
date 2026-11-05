# Third-party components

## FFmpeg (bundled into the executable)

The packaged `RotateVideo.exe` contains a static FFmpeg build
(`ffmpeg version 7.1-essentials_build`, from <https://www.gyan.dev/ffmpeg/builds/>).

That build is compiled with:

```
--enable-gpl --enable-version3 --enable-libx264 --enable-libx265 ...
```

so it is distributed under the **GNU General Public License v3 (GPLv3)**.

What this means in practice:

- **ใช้ส่วนตัว / ภายในองค์กร** — ไม่มีปัญหา
- **แจกจ่าย exe ให้บุคคลภายนอก** — ต้องปฏิบัติตาม GPLv3 เช่น
  - ให้ข้อมูล/Link แหล่งที่มาของ FFmpeg (source code) ที่ใช้ build นี้
  - ไม่จำกัดสิทธิ์ผู้รับในการศึกษาการทำงานของโปรแกรม
  - แจกจ่ายไฟล์ที่แก้ไขภายใต้ GPLv3 เช่นกัน
- **ต้องการแจกจ่ายเชิงพาณิชย์แบบปิด source** — ต้องใช้ FFmpeg build แบบ LGPL
  ที่ไม่มี libx264 (ต้องหา encoder อื่น) หรือขอคำปรึกษาทางกฎหมาย

แหล่งข้อมูล:

- FFmpeg source: <https://ffmpeg.org/download.html>
- GPLv3 text: <https://www.gnu.org/licenses/gpl-3.0.html>
- Gyan.dev builds: <https://www.gyan.dev/ffmpeg/builds/>

## Python / PyInstaller / Tk

- Python: PSF License
- PyInstaller: GPLv2-or-later **with bootloader exception** (ไม่ทำให้โปรแกรมที่แพ็กต้องเป็น GPL)
- Tkinter / Tcl-Tk: BSD-style license
- windnd: MIT
