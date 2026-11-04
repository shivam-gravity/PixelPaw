"""🐾 PixelPaw: double-click to start (runs in the background, opens the dashboard in your browser).

First time on Windows, double-click PixelPaw.cmd instead: it installs what's needed, then opens this.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# Always run inside PixelPaw's own environment (.venv), without a console window.
venv_python = ROOT / ".venv" / ("Scripts/pythonw.exe" if os.name == "nt" else "bin/python")
if venv_python.exists() and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]], cwd=ROOT,
                     creationflags=flags, close_fds=True)
    sys.exit(0)

try:
    import yaml  # noqa: F401  (the only dependency)
except ImportError:
    installer = ROOT / ("PixelPaw.cmd" if os.name == "nt" else "PixelPaw.command")
    if os.name == "nt":
        os.startfile(str(installer))  # shows the one-time installer window
    else:
        subprocess.Popen(["sh", str(installer)], cwd=ROOT)
    sys.exit(0)

from pixelpaw.launcher import main  # noqa: E402

sys.exit(main())
