"""Install the PixelPaw OBS plugin (obs/plugin) into OBS and point it at this PixelPaw.

    python -m pixelpaw.obsplugin install | uninstall | status
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from .config import ROOT, load_settings

NAME = "pixelpaw"
PACKAGE = ROOT / "obs" / "plugin" / "dist" / NAME  # made by obs/plugin/build.ps1 (or shipped in a release)


def obs_config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "obs-studio"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "obs-studio"
    flatpak = Path.home() / ".var" / "app" / "com.obsproject.Studio" / "config" / "obs-studio"
    return flatpak if flatpak.exists() else Path.home() / ".config" / "obs-studio"


def plugins_dir() -> Path:
    if sys.platform == "win32":  # OBS 30+ loads per-machine plugins from here, no admin rights needed
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "obs-studio" / "plugins"
    return obs_config_dir() / "plugins"


def target() -> Path:
    return plugins_dir() / NAME


def config_file() -> Path:
    return obs_config_dir() / "plugin_config" / NAME / "config.json"


def status() -> dict:
    return {"built": PACKAGE.exists(), "installed": target().exists(), "path": str(target()), "config": str(config_file())}


def write_config(autostart: bool = True) -> Path:
    s = load_settings()
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"folder": str(ROOT), "url": f"http://127.0.0.1:{s.web_port}", "autostart": autostart}, indent=2),
                    encoding="utf-8")
    return path


def install() -> dict:
    if not PACKAGE.exists():
        raise FileNotFoundError("The OBS plugin isn't built yet. On Windows run: obs\\plugin\\build.ps1")
    dest = target()
    try:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(PACKAGE, dest)
    except PermissionError as exc:
        raise PermissionError("OBS is using the plugin right now: close OBS, then install again.") from exc
    write_config()
    return {**status(), "message": "Installed. Restart OBS: PixelPaw docks appear next to Controls and in the chat area."}


def uninstall() -> dict:
    try:
        if target().exists():
            shutil.rmtree(target())
    except PermissionError as exc:
        raise PermissionError("Close OBS first, then uninstall again.") from exc
    return status()


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    try:
        result = {"install": install, "uninstall": uninstall, "status": status}[cmd]()
    except KeyError:
        print(__doc__)
        return 2
    except OSError as exc:
        print(f"✖ {exc}")
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
