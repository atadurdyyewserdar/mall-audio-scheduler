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
    # Do NOT --collect-all PySide6: it drags in WebEngine, QML, Designer and
    # headers (~700 MB) and produces paths longer than Windows' 260-char limit,
    # which breaks Inno Setup. PyInstaller's PySide6 hooks bundle the Qt
    # modules the code imports plus their plugins (including the multimedia
    # backend) automatically.
    "--hidden-import", "PySide6.QtMultimedia",
    "--collect-data", "qtawesome",
    "--exclude-module", "PySide6.QtWebEngineCore",
    "--exclude-module", "PySide6.QtWebEngineWidgets",
    "--exclude-module", "PySide6.QtQml",
    "--exclude-module", "PySide6.QtQuick",
    "--exclude-module", "PySide6.Qt3DCore",
    "--exclude-module", "PySide6.QtCharts",
    "--exclude-module", "PySide6.QtDataVisualization",
    "--exclude-module", "PySide6.QtPdf",
    "--exclude-module", "PySide6.QtDesigner",
    "--paths", str(ROOT),
    # Ship the logo next to the module so asset_path() finds it in the bundle.
    "--add-data", f"{ROOT / 'mall_audio' / 'assets'}{os.pathsep}assets",
]
if ICON.exists():
    command += ["--icon", str(ICON)]
# Entry shim: app.py uses relative imports, which fail when PyInstaller runs
# it as a top-level script. launch.py imports it as a package module instead.
command.append(str(ROOT / "scripts" / "launch.py"))
subprocess.run(command, check=True, cwd=ROOT)
