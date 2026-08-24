from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def app_root() -> Path:
    # PyInstaller one-file extracts the launcher, but sys.executable remains the
    # stable ManualSync.exe path in the distribution root.
    return Path(sys.executable).resolve().parent


def main() -> int:
    root = app_root()
    runtime = root / "runtime"
    app = root / "app"
    # Only the supported ManualSync switches are forwarded. Older desktop
    # shortcuts used to carry a legacy desktop_launcher.py argument; forwarding
    # that stale value made argparse abort before the UI could start.
    supported = {"--resource-check", "--headless-smoke", "--acceptance-smoke"}
    forwarded_args = [a for a in sys.argv[1:] if a in supported]
    debug_mode = bool(forwarded_args)
    python = runtime / ("python.exe" if debug_mode else "pythonw.exe")
    script = app / "manualsync_desktop.py"

    if not python.exists() or not script.exists():
        # Keep this dependency-free and usable even in a broken installation.
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                f"Installazione ManualSync incompleta.\n\nManca: {python if not python.exists() else script}",
                "ManualSync — errore di installazione",
                0x10,
            )
        except Exception:
            pass
        return 2

    cmd = [str(python), str(script), *forwarded_args]
    env = os.environ.copy()
    env["MANUALSYNC_DISTRIBUTION_ROOT"] = str(root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"

    if debug_mode:
        return subprocess.call(cmd, cwd=str(app), env=env)

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # Keep ManualSync.exe alive for the full desktop session. Besides making
    # process lifecycle deterministic, this lets installer/uninstaller reliably
    # detect an open application instead of seeing only a short-lived launcher.
    return subprocess.call(
        cmd,
        cwd=str(app),
        env=env,
        creationflags=creationflags,
        close_fds=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
