"""Where the browser gates find Chrome -- on any Windows machine, never one path.

TEST INFRASTRUCTURE ONLY -- no product module imports this file, so it is
never part of the packaged application.

Why this exists
---------------
The browser gates used to start a constant, ``C:\\Program Files\\Google\\
Chrome\\Application\\chrome.exe``. On a clean machine where Chrome lives in
``Program Files (x86)`` (CP2.4, 12/10/2026) all four browser suites failed
with ``FileNotFoundError`` before testing anything: an environment
difference reported as four product failures.

Resolution order -- the first candidate that exists wins:

1. ``ERYLOS_CHROME``: an explicit path for CI or an unusual install. If it is
   set it is authoritative: a wrong value is an error, never a silent
   fallback to some other Chrome.
2. The standard install locations, built from the environment, never from
   this machine's drive letter: ``%ProgramFiles%``, ``%ProgramW6432%``,
   ``%ProgramFiles(x86)%`` (machine-wide) and ``%LOCALAPPDATA%`` (per-user).
3. The ``App Paths\\chrome.exe`` registration (HKLM, then HKCU), which
   covers relocated installs.
4. Otherwise ``ChromeNotFoundError``, naming every place that was tried.

Environment and registry are parameters so the mutation suite
(regressione_chrome_locator.py) can exercise every branch without Chrome.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Mapping

ENV_VAR = "ERYLOS_CHROME"
_RELATIVE = Path("Google") / "Chrome" / "Application" / "chrome.exe"
_STANDARD_ROOTS = ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA")
_APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"


class ChromeNotFoundError(FileNotFoundError):
    """No usable Chrome: the message lists every location that was tried."""


def _registry_app_paths() -> list[str]:
    """Default values of the App Paths registration, HKLM first. Empty off Windows."""
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return []
    found = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, _APP_PATHS) as key:
                value, _ = winreg.QueryValueEx(key, "")
                if value:
                    found.append(str(value).strip('"'))
        except OSError:
            continue
    return found


def candidates(env: Mapping[str, str] | None = None,
               registry: Callable[[], list[str]] | None = None) -> list[Path]:
    """Every location considered, in resolution order (ERYLOS_CHROME excluded)."""
    env = os.environ if env is None else env
    registry = _registry_app_paths if registry is None else registry
    out: list[Path] = []
    for var in _STANDARD_ROOTS:
        root = env.get(var)
        if root:
            out.append(Path(root) / _RELATIVE)
    out.extend(Path(p) for p in registry())
    seen, unique = set(), []
    for p in out:
        key = str(p).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def find_chrome(env: Mapping[str, str] | None = None,
                registry: Callable[[], list[str]] | None = None) -> Path:
    """The Chrome executable the browser gates must drive."""
    env = os.environ if env is None else env
    explicit = env.get(ENV_VAR)
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise ChromeNotFoundError(f"{ENV_VAR} points to a file that does not exist: {p}")
    tried = candidates(env, registry)
    for p in tried:
        if p.is_file():
            return p
    listing = "\n".join(f"  - {p}" for p in tried) or "  (no candidate location could be built)"
    raise ChromeNotFoundError(
        "Google Chrome was not found. Install it, or set "
        f"{ENV_VAR} to the full path of chrome.exe. Locations tried:\n{listing}")
