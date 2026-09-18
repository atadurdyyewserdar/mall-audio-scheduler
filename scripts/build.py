"""Create a native distributable for the current operating system.

Run this ON the operating system you are building for: PyInstaller cannot
cross-compile, so the Windows app is built on Windows and the macOS app on
macOS. The output lands in dist/Mall Audio Scheduler/.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "mall_audio" / "assets" / ("logo.ico" if sys.platform == "win32" else "logo.icns")

command = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm",
    "--windowed",
    "--name", "Mall Audio Scheduler",
    "--collect-all", "PySide6",
    "--hidden-import", "PySide6.QtMultimedia",
    "--paths", str(ROOT),
    # Ship the logo next to the module so asset_path() finds it in the bundle.
    "--add-data", f"{ROOT / 'mall_audio' / 'assets'}{os.pathsep}assets",
]
if ICON.exists():
    command += ["--icon", str(ICON)]
command.append(str(ROOT / "mall_audio" / "app.py"))
subprocess.run(command, check=True, cwd=ROOT)
