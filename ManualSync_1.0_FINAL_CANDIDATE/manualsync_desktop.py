from __future__ import annotations

import argparse
import ctypes
import importlib.resources
import os
import socket
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from project_layout import default_data_home

HOST = "127.0.0.1"
APP_NAME = "ManualSync"
MUTEX_NAME = r"Local\ManualSyncDesktopApp"
_MUTEX_HANDLE = None
_STDOUT_HANDLE = None
_STDERR_HANDLE = None


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_home() -> Path:
    p = default_data_home()
    p.mkdir(parents=True, exist_ok=True)
    return p


LOG_DIR = data_home() / "Logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "ManualSync_Desktop.log"
ERROR_FILE = LOG_DIR / "ManualSync_Desktop_Error.log"
SMOKE_ERROR_FILE = LOG_DIR / "ManualSync_Packaged_Smoke_Error.log"


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {message}\n")


def show_message(title: str, message: str, error: bool = False) -> None:
    if os.name == "nt":
        flags = 0x10 if error else 0x40
        try:
            ctypes.windll.user32.MessageBoxW(None, message, title, flags)
            return
        except Exception:
            pass
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        print(f"{title}: {message}", file=stream)


def ensure_stdio() -> None:
    """Provide real file-backed stdout/stderr when launched with pythonw.exe.

    On Windows, pythonw.exe deliberately starts without console streams, so
    sys.stdout/sys.stderr can be None. Uvicorn's default logging formatter calls
    .isatty() while configuring logging; without valid streams the server aborts
    before opening a port. Keep the handles alive for the whole process.
    """
    global _STDOUT_HANDLE, _STDERR_HANDLE
    if sys.stdout is None:
        _STDOUT_HANDLE = LOG_FILE.open("a", encoding="utf-8", buffering=1)
        sys.stdout = _STDOUT_HANDLE
    if sys.stderr is None:
        _STDERR_HANDLE = ERROR_FILE.open("a", encoding="utf-8", buffering=1)
        sys.stderr = _STDERR_HANDLE


def prepare_runtime() -> None:
    """Prepare a deterministic writable runtime before importing Gradio.

    In frozen builds we deliberately do not use the user's extracted BuildKit as
    Gradio's cwd or temp directory. This prevents packaged resources/cache paths
    from accidentally pointing back to the source/build folder.
    """
    ensure_stdio()
    run_dir = runtime_dir()
    os.chdir(run_dir)
    temp_dir = data_home() / "Temp" / "Gradio"
    temp_dir.mkdir(parents=True, exist_ok=True)
    os.environ["GRADIO_TEMP_DIR"] = str(temp_dir)
    # Assigned, not setdefault: a GRADIO_ANALYTICS_ENABLED=True already in the
    # customer's environment must not switch Gradio's telemetry back on.
    os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
    log(
        f"Runtime prepared frozen={bool(getattr(sys, 'frozen', False))} "
        f"cwd={run_dir} meipass={getattr(sys, '_MEIPASS', '')} gradio_temp={temp_dir}"
    )


def packaged_resource_check() -> list[str]:
    """Return diagnostic lines for resources required to start the UI."""
    diagnostics: list[str] = []
    icon = resource_path("manualsync.png")
    diagnostics.append(f"manualsync.png exists={icon.exists()} path={icon}")
    # Locale catalogues. i18n.load_catalog degrades to raw keys when these
    # are missing rather than refusing to start, so without these lines a
    # damaged installation passes every startup check and shows
    # "nav.conformity" where a label belongs. exists=False fails the check.
    for lang in ("it", "en"):
        catalog = resource_path(f"locales/{lang}.json")
        diagnostics.append(f"locales/{lang}.json exists={catalog.exists()} path={catalog}")
    try:
        gradio_root = Path(str(importlib.resources.files("gradio")))
        diagnostics.append(f"gradio_root exists={gradio_root.exists()} path={gradio_root}")
        for rel in ("templates", "templates/frontend", "templates/frontend/assets", "templates/frontend/static"):
            p = gradio_root / rel
            diagnostics.append(f"gradio/{rel} exists={p.exists()} path={p}")
    except Exception as exc:
        diagnostics.append(f"gradio resource inspection FAILED: {type(exc).__name__}: {exc}")

    # Gradio 6 imports safehttpx at startup. safehttpx reads version.txt via
    # Path(__file__).parent at import time, so PyInstaller must bundle it explicitly.
    try:
        safehttpx_root = Path(str(importlib.resources.files("safehttpx")))
        version_file = safehttpx_root / "version.txt"
        diagnostics.append(f"safehttpx/version.txt exists={version_file.exists()} path={version_file}")
        if version_file.exists():
            diagnostics.append(f"safehttpx/version={version_file.read_text(encoding='utf-8').strip()}")
    except Exception as exc:
        diagnostics.append(f"safehttpx resource inspection FAILED: {type(exc).__name__}: {exc}")

    try:
        client_root = Path(str(importlib.resources.files("gradio_client")))
        for rel in ("package.json", "types.json"):
            p = client_root / rel
            diagnostics.append(f"gradio_client/{rel} exists={p.exists()} path={p}")
    except Exception as exc:
        diagnostics.append(f"gradio_client resource inspection FAILED: {type(exc).__name__}: {exc}")
    return diagnostics


def acquire_single_instance() -> bool:
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise OSError("Impossibile creare il mutex di istanza ManualSync")
    _MUTEX_HANDLE = handle
    ERROR_ALREADY_EXISTS = 183
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        show_message(APP_NAME, "ManualSync è già aperto.")
        return False
    return True


def choose_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        s.listen(1)
        return int(s.getsockname()[1])


def wait_http(url: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if int(getattr(r, "status", 200)) < 500:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def start_server(port: int):
    prepare_runtime()
    import gradio as gr
    from app import CSS, DATA_ROOT, demo

    icon = resource_path("manualsync.png")
    _, local_url, _ = demo.launch(
        server_name=HOST,
        server_port=port,
        inbrowser=False,
        prevent_thread_lock=True,
        show_error=False,
        quiet=True,
        theme=gr.themes.Base(),
        css=CSS,
        favicon_path=str(icon) if icon.exists() else None,
        allowed_paths=[str(DATA_ROOT.resolve())],
        footer_links=[],
    )
    return demo, local_url


def run_resource_check() -> int:
    try:
        prepare_runtime()
        # Import after runtime setup, matching the real packaged startup order.
        import gradio  # noqa: F401
        lines = packaged_resource_check()
        for line in lines:
            print(line)
            log("RESOURCE_CHECK " + line)
        missing = [x for x in lines if "exists=False" in x or "FAILED" in x]
        return 1 if missing else 0
    except BaseException as exc:
        SMOKE_ERROR_FILE.write_text(traceback.format_exc(), encoding="utf-8")
        log(f"RESOURCE_CHECK FAILED: {type(exc).__name__}: {exc}")
        if sys.stderr is not None:
            print(traceback.format_exc(), file=sys.stderr)
        return 1


def run_headless_smoke() -> int:
    port = choose_free_port()
    log(f"HEADLESS_SMOKE start port={port}")
    demo = None
    try:
        demo, url = start_server(port)
        if not wait_http(url, timeout=60):
            raise RuntimeError(f"Server non raggiungibile su {url}")
        log(f"HEADLESS_SMOKE PASS url={url}")
        if sys.stdout is not None:
            print(f"PASS {url}")
        return 0
    except BaseException as exc:
        tb = traceback.format_exc()
        SMOKE_ERROR_FILE.write_text(tb, encoding="utf-8")
        log(f"HEADLESS_SMOKE FAILED: {type(exc).__name__}: {exc}")
        if sys.stderr is not None:
            print(tb, file=sys.stderr)
        return 1
    finally:
        if demo is not None:
            try:
                demo.close()
            except Exception:
                pass



def run_acceptance_smoke() -> int:
    """Execute the packaged commercial acceptance suite in isolated temp data."""
    try:
        prepare_runtime()
        from commercial_acceptance import run_acceptance
        report_dir = data_home() / "Logs" / "CommercialAcceptance"
        payload = run_acceptance(report_dir=report_dir)
        status = str(payload.get("status", "FAIL"))
        log(f"COMMERCIAL_ACCEPTANCE {status} pass={payload.get('pass')} tests={payload.get('tests')} report={report_dir}")
        if sys.stdout is not None:
            print(f"COMMERCIAL_ACCEPTANCE {status} {payload.get('pass')}/{payload.get('tests')} report={report_dir}")
        return 0 if status == "PASS" else 1
    except BaseException as exc:
        tb = traceback.format_exc()
        SMOKE_ERROR_FILE.write_text(tb, encoding="utf-8")
        log(f"COMMERCIAL_ACCEPTANCE FAILED: {type(exc).__name__}: {exc}")
        if sys.stderr is not None:
            print(tb, file=sys.stderr)
        return 1

def run_desktop() -> int:
    if not acquire_single_instance():
        return 0
    port = choose_free_port()
    log(f"Desktop start port={port}")
    demo = None
    try:
        demo, url = start_server(port)
        if not wait_http(url, timeout=60):
            raise RuntimeError(f"ManualSync non è diventato pronto entro 60 secondi ({url})")

        try:
            import webview
        except Exception as e:
            raise RuntimeError(
                "Il componente desktop pywebview non è disponibile. Reinstallare ManualSync."
            ) from e

        # pywebview blocks downloads by default. ManualSync must allow
        # Gradio File/DownloadButton outputs to save XLIFF/ZIP/report artifacts.
        webview.settings["ALLOW_DOWNLOADS"] = True
        log(f"Opening native window url={url} downloads=enabled")
        webview.create_window(
            APP_NAME,
            url=url,
            width=1440,
            height=940,
            min_size=(1100, 720),
            maximized=True,
        )
        webview.start(debug=False)
        log("Native window closed by user")
        return 0
    except BaseException as e:
        ERROR_FILE.write_text(traceback.format_exc(), encoding="utf-8")
        log(f"Desktop startup FAILED: {type(e).__name__}: {e}")
        show_message(
            "ManualSync — errore di avvio",
            f"ManualSync non si è avviato.\n\n{type(e).__name__}: {e}\n\nDettagli: {ERROR_FILE}",
            error=True,
        )
        return 1
    finally:
        if demo is not None:
            try:
                demo.close()
                log("Local server closed")
            except Exception:
                pass


def main() -> int:
    ensure_stdio()
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--resource-check", action="store_true", help="Verifica risorse del bundle e termina")
    parser.add_argument("--headless-smoke", action="store_true", help="Avvia server, verifica HTTP e termina")
    parser.add_argument("--acceptance-smoke", action="store_true", help="Esegue il collaudo commerciale isolato e termina")
    args, unknown = parser.parse_known_args()
    if unknown:
        log(f"Ignored legacy/unknown launcher arguments: {unknown!r}")
    if args.resource_check:
        return run_resource_check()
    if args.headless_smoke:
        return run_headless_smoke()
    if args.acceptance_smoke:
        return run_acceptance_smoke()
    return run_desktop()


if __name__ == "__main__":
    raise SystemExit(main())
