"""Small, dependency-free operating-system auto-start integration."""
from __future__ import annotations

import plistlib
import sys
from pathlib import Path


LABEL = "com.mallaudio.scheduler"


def _project_root() -> Path:
    """Where `-m mall_audio.app` must be run from when not frozen into an app."""
    return Path(__file__).resolve().parent.parent


def _command() -> str:
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    # Windows Run entries start in a directory of the OS's choosing, so the
    # package is put on sys.path explicitly rather than relying on the cwd.
    # pythonw keeps a console window from opening at every login.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    snippet = (
        "import runpy, sys; sys.path.insert(0, r'%s'); "
        "runpy.run_module('mall_audio.app', run_name='__main__')" % _project_root()
    )
    return '"%s" -c "%s"' % (interpreter, snippet)


def is_enabled() -> bool:
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist").exists()
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                winreg.QueryValueEx(key, "MallAudioScheduler")
                return True
        except OSError:
            return False
    return False


def set_enabled(enabled: bool) -> None:
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            frozen = getattr(sys, "frozen", False)
            program_args = [sys.executable] if frozen else [sys.executable, "-m", "mall_audio.app"]
            agent = {
                "Label": LABEL,
                "ProgramArguments": program_args,
                # Start at login, and come back if the process dies. SuccessfulExit
                # false means launchd relaunches only after a crash or kill, not
                # after the operator quits the app on purpose.
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "ProcessType": "Interactive",
            }
            if not frozen:
                # launchd runs agents from /, where `-m mall_audio.app` cannot be
                # found; the old entry silently failed to start at every login.
                agent["WorkingDirectory"] = str(_project_root())
            with path.open("wb") as file:
                plistlib.dump(agent, file)
        elif path.exists():
            path.unlink()
    elif sys.platform == "win32":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, "MallAudioScheduler", 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(key, "MallAudioScheduler")
                except FileNotFoundError:
                    pass
