"""Start-with-computer and desktop shortcut, per OS. No extra dependencies.

Windows: shortcuts are created with the built-in Windows Script Host (cscript).
macOS:   a LaunchAgent.  Linux: an XDG autostart entry.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import ROOT

APP_NAME = "PixelPaw"


def _venv_python(windowless: bool = True) -> Path:
    if os.name == "nt":
        exe = ROOT / ".venv" / "Scripts" / ("pythonw.exe" if windowless else "python.exe")
    else:
        exe = ROOT / ".venv" / "bin" / "python"
    return exe if exe.exists() else Path(sys.executable)


def launch_command(no_browser: bool = True) -> list[str]:
    cmd = [str(_venv_python()), str(ROOT / "PixelPaw.pyw")]
    return cmd + (["--no-browser"] if no_browser else [])


# ---- Windows ------------------------------------------------------------------
def _startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _make_lnk(link: Path, target: Path, args: str, workdir: Path, description: str) -> None:
    def q(s: str) -> str:
        return str(s).replace('"', '""')

    vbs = "\r\n".join([
        'Set sh = CreateObject("WScript.Shell")',
        f'Set lnk = sh.CreateShortcut("{q(link)}")',
        f'lnk.TargetPath = "{q(target)}"',
        f'lnk.Arguments = "{q(args)}"',
        f'lnk.WorkingDirectory = "{q(workdir)}"',
        f'lnk.Description = "{q(description)}"',
        "lnk.WindowStyle = 7",
        "lnk.Save",
    ])
    with tempfile.NamedTemporaryFile("w", suffix=".vbs", delete=False, encoding="utf-16") as f:
        f.write(vbs)
        script = f.name
    try:
        subprocess.run(["cscript", "//nologo", script], check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30)
    finally:
        os.unlink(script)


# ---- start with computer ----------------------------------------------------------
def _paths() -> dict:
    home = Path.home()
    return {
        "nt": _startup_dir() / f"{APP_NAME}.lnk",
        "darwin": home / "Library" / "LaunchAgents" / "com.pixelpaw.agent.plist",
        "linux": home / ".config" / "autostart" / "pixelpaw.desktop",
    }


def _platform() -> str:
    return "nt" if os.name == "nt" else "darwin" if sys.platform == "darwin" else "linux"


def is_enabled() -> bool:
    return _paths()[_platform()].exists()


def enable() -> Path:
    plat, path = _platform(), _paths()[_platform()]
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = launch_command(no_browser=True)
    if plat == "nt":
        _make_lnk(path, Path(cmd[0]), " ".join(f'"{c}"' for c in cmd[1:]), ROOT, "Start PixelPaw in the background")
    elif plat == "darwin":
        args = "".join(f"<string>{c}</string>" for c in cmd)
        path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.pixelpaw.agent</string>
<key>ProgramArguments</key><array>{args}</array>
<key>WorkingDirectory</key><string>{ROOT}</string>
<key>RunAtLoad</key><true/>
</dict></plist>
""", encoding="utf-8")
    else:
        exec_line = " ".join(f'"{c}"' for c in cmd)
        path.write_text(f"[Desktop Entry]\nType=Application\nName={APP_NAME}\nExec={exec_line}\nPath={ROOT}\n"
                        "X-GNOME-Autostart-enabled=true\n", encoding="utf-8")
    return path


def disable() -> None:
    path = _paths()[_platform()]
    if path.exists():
        path.unlink()


def create_desktop_shortcut() -> Path:
    desktop = Path.home() / "Desktop"
    if os.name == "nt":
        link = desktop / f"{APP_NAME}.lnk"
        cmd = launch_command(no_browser=False)
        _make_lnk(link, Path(cmd[0]), f'"{cmd[1]}"', ROOT, "Open PixelPaw")
        return link
    if sys.platform == "darwin":
        link = desktop / f"{APP_NAME}.command"
        link.write_text(f'#!/bin/sh\ncd "{ROOT}" && ./PixelPaw.command\n', encoding="utf-8")
        link.chmod(0o755)
        return link
    link = desktop / "pixelpaw.desktop"
    link.write_text(f'[Desktop Entry]\nType=Application\nName={APP_NAME}\nExec="{ROOT}/PixelPaw.command"\nPath={ROOT}\n',
                    encoding="utf-8")
    link.chmod(0o755)
    return link
