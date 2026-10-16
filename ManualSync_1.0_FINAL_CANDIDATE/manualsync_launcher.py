from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def app_root() -> Path:
    # PyInstaller one-file extracts the launcher, but sys.executable remains the
    # stable ManualSync.exe path in the distribution root.
    return Path(sys.executable).resolve().parent


def exit_status(returncode: int) -> int:
    """The child's Windows exit status, in a form SystemExit hands back unchanged.

    subprocess reports a Windows status as an UNSIGNED 32-bit number. A child
    the system ends -- a crash (0xC0000005), a closed console or Ctrl+C
    (0xC000013A), a failed DLL start (0xC0000142) -- therefore returns a value
    >= 2**31, and `raise SystemExit(3221225786)` cannot be converted to a C
    long on Windows: Python reports an OverflowError (invisible in this
    windowed exe) and the process exits with -1. The real reason is lost.
    That is exactly what a CP2.4 build gate saw: "exit -1", nothing in any log
    (CP2.5-A). As a signed 32-bit value the same status passes through intact,
    so ManualSync.exe ends with the very code its Python child ended with.
    """
    return returncode - (1 << 32) if returncode >= (1 << 31) else returncode


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

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    if debug_mode:
        # No console window for the diagnostic child either (CP2.5-A.1). This
        # GUI launcher used to start python.exe without it, so Windows gave the
        # child a console of its own on the desktop, and any console event --
        # the window closed, Ctrl+C/Break, a logoff -- killed it in the middle
        # of a build or installer gate (STATUS_CONTROL_C_EXIT; reproduced in
        # CP2.5-A with exactly the "-1 after ~3 s, nothing in the log" of a
        # CP2.4 build). Nobody reads that console: every caller checks only the
        # exit code, and manualsync_desktop.ensure_stdio() already sends stdout
        # and stderr to the desktop log when there is no console -- as for the
        # desktop session below, which has always run this way.
        return subprocess.call(cmd, cwd=str(app), env=env, creationflags=creationflags)

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
    # The one exit point of ManualSync.exe: every status main() returns -- the
    # Python child's, or 2 for an incomplete installation -- leaves through
    # exit_status(), so a status set by the system is never collapsed to -1.
    raise SystemExit(exit_status(main()))
