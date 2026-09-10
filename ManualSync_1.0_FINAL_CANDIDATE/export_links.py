"""Milestone 4F.1 (export UX): opaque, read-only HTTP access to generated
export artifacts.

Why this exists
---------------
The first 4F.1 cut handed the user a raw Gradio File card and printed the
technical export path in standard mode. Making "Apri PDF" a real link
instead needs a URL -- and Gradio's own `/gradio_api/file=<abs path>`
route IS the absolute filesystem path, so using it would put
`C:\\Users\\<name>\\...` into the page for every export.

This module is the alias layer that avoids that: an export registers its
artifacts once and gets back an unguessable token; the browser only ever
sees `/ms-export/<token>/f/index.html`. No project name, no publication
name, no profile, no username, no filesystem path anywhere in the URL,
the DOM or the network log.

Security model (fail closed at every step)
------------------------------------------
1. Only paths handed to register_export() are reachable at all -- there
   is no way to name an arbitrary file, because the URL carries a token,
   not a path.
2. The token is 128+ bits from `secrets`, so it cannot be guessed or
   enumerated.
3. Every request re-resolves `root / subpath` and re-checks containment
   with `relative_to(root)` AFTER resolution, so `..` traversal inside
   the subpath cannot escape the registered directory.
4. Only the extensions this app actually produces are served; anything
   else is 404, never "download whatever is on disk".
5. Read-only: GET only, `FileResponse` only, no write/delete route.
6. The registry is in-memory and session-scoped -- nothing is persisted,
   and a restart invalidates every previously issued token.

Routes are installed lazily on the ALREADY-RUNNING Gradio FastAPI app
(`demo.app`), which is why this works for both entry points (`app.py`'s
blocking launch and manualsync_desktop's) without changing either
launch call: by the time an export callback can run, the server exists.
Starlette matches routes per request, so adding one after startup is
picked up immediately (verified against this pinned Gradio build).
"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

_ROUTE_PREFIX = "/ms-export"

# Only what this app's own exporters produce. Deliberately not the OS
# mimetypes registry (machine-dependent, user-modifiable), and no SVG:
# an SVG can carry <script>, and these files are served same-origin.
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".zip": "application/zip",
}

# Bounded so a long session cannot grow the registry without limit.
_MAX_ENTRIES = 64

_REGISTRY: dict[str, dict[str, Any]] = {}
_ROUTES_INSTALLED = False


def register_export(*, root: Path, primary_rel: str, download_path: Path,
                    download_name: str) -> str:
    """Registers one exported artifact set and returns its opaque token.

    root          directory that may be served (the export folder)
    primary_rel   relative file inside root to OPEN (e.g. "index.html")
    download_path file to send as an attachment (the PDF, or the zip)
    download_name filename the browser should save it as
    """
    root = Path(root).resolve()
    token = secrets.token_urlsafe(18)
    if len(_REGISTRY) >= _MAX_ENTRIES:
        for stale in list(_REGISTRY)[: len(_REGISTRY) - _MAX_ENTRIES + 1]:
            _REGISTRY.pop(stale, None)
    _REGISTRY[token] = {
        "root": root,
        "primary": str(primary_rel).replace("\\", "/").lstrip("/"),
        "download_path": Path(download_path).resolve() if download_path else None,
        "download_name": download_name,
    }
    return token


def open_url(token: str) -> str:
    entry = _REGISTRY.get(token)
    if not entry:
        return ""
    return f"{_ROUTE_PREFIX}/{token}/f/{entry['primary']}"


def download_url(token: str) -> str:
    entry = _REGISTRY.get(token)
    if not entry or not entry.get("download_path"):
        return ""
    return f"{_ROUTE_PREFIX}/{token}/download"


def resolve_served_file(token: str, subpath: str) -> Path | None:
    """The containment gate, factored out so it is unit-testable without
    an HTTP server. Returns the real file to serve, or None (-> 404) for
    an unknown token, a traversal attempt, a path outside the registered
    root, a missing file or an extension this app never produces."""
    entry = _REGISTRY.get(token)
    if not entry:
        return None
    root: Path = entry["root"]
    try:
        candidate = (root / str(subpath or "")).resolve()
        candidate.relative_to(root)
    except (ValueError, OSError):
        return None
    if not candidate.is_file():
        return None
    if candidate.suffix.lower() not in _MEDIA_TYPES:
        return None
    return candidate


def resolve_download(token: str) -> tuple[Path, str] | None:
    entry = _REGISTRY.get(token)
    if not entry:
        return None
    path = entry.get("download_path")
    if not path or not Path(path).is_file():
        return None
    if Path(path).suffix.lower() not in _MEDIA_TYPES:
        return None
    return Path(path), str(entry.get("download_name") or Path(path).name)


def install_routes(fastapi_app: Any) -> bool:
    """Idempotently installs the two read-only routes on the running
    Gradio FastAPI app. Returns True once routes are available."""
    global _ROUTES_INSTALLED
    if _ROUTES_INSTALLED:
        return True
    if fastapi_app is None:
        return False
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    @fastapi_app.get(_ROUTE_PREFIX + "/{token}/download")
    def _ms_export_download(token: str):  # pragma: no cover - exercised over HTTP
        found = resolve_download(token)
        if not found:
            raise HTTPException(status_code=404)
        path, name = found
        return FileResponse(str(path), media_type=_MEDIA_TYPES.get(path.suffix.lower()),
                            filename=name)

    @fastapi_app.get(_ROUTE_PREFIX + "/{token}/f/{subpath:path}")
    def _ms_export_file(token: str, subpath: str):  # pragma: no cover - exercised over HTTP
        path = resolve_served_file(token, subpath)
        if not path:
            raise HTTPException(status_code=404)
        # inline (no `filename=`): the browser renders the PDF/HTML in a
        # tab instead of downloading it -- that IS the "Apri" action.
        return FileResponse(str(path), media_type=_MEDIA_TYPES.get(path.suffix.lower()))

    _ROUTES_INSTALLED = True
    return True


def _reset_for_tests() -> None:
    _REGISTRY.clear()
