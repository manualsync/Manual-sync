"""What the Windows distribution must contain — derived, never listed.

Why this module exists
----------------------
BUILD_WINDOWS.ps1 used to carry a hand-written allowlist of the files to
copy into ``dist/ManualSync/app``. Between Gate 2A and Impact Center the
product grew twenty-one modules and a ``locales`` directory; the
allowlist grew nothing. Two failure modes followed, and they are not
equally visible:

* a missing MODULE stops the build -- ``app.py`` cannot import, and the
  build's own smoke test refuses to produce a package. Loud, and safe.
* a missing DATA directory does not. ``i18n.load_catalog`` deliberately
  degrades to an empty catalogue so a packaging mistake "degrades to
  visible keys instead of an app that will not start". That design is
  right for robustness and fatal for packaging: resource-check, headless
  smoke and commercial acceptance all pass, the customer installs
  successfully, and every label in the product reads ``nav.conformity``.

So the payload is computed here from the code itself, and verified
statically. A list nobody maintains cannot go stale.

The contract
------------
``runtime_modules()``  transitive import closure of the real entry points
``runtime_data()``     data files and directories read at runtime
``payload_entries()``  everything, relative to this directory
``verify(app_dir)``    what is missing from a materialised payload

Command line, for PowerShell and for humans:

    python packaging_payload.py --print          one relative path per line
    python packaging_payload.py --verify <dir>   exit 1 and name what is missing
    python packaging_payload.py --explain        why each entry is included
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent

# The processes that actually start on a customer machine. manualsync_desktop
# is the one the launcher executes; app builds the UI; commercial_acceptance
# is run by --acceptance-smoke, on the installed product, so it ships too.
ENTRY_POINTS = ("manualsync_desktop", "app", "commercial_acceptance")

# Data the running product reads. Directories are copied whole.
#   locales/       i18n catalogues -- see the module docstring above
#   fonts/         the interface fonts (and their OFL licences), embedded in
#                  the stylesheet at startup: the product must look the same
#                  offline and must not call a font server at every start
#   manualsync.png window/tray icon, asserted by packaged_resource_check()
#   VERSION.txt    shown in Impostazioni and read by the installer
#   *_esempio.*    sample terminology/QA/vendor files the UI offers to load
RUNTIME_DATA_DIRS = ("locales", "fonts")
RUNTIME_DATA_FILES = (
    "manualsync.png",
    "VERSION.txt",
    "qa_profile_esempio.json",
    "glossario_equivalenze_esempio.csv",
    "terminologia_cat_esempio.csv",
    "vendor_rates_esempio.csv",
)

# manualsync_launcher is NOT here on purpose: PyInstaller freezes it into
# ManualSync.exe from ManualSyncLauncher.spec, so shipping the source as
# well would put two copies of the entry point in the distribution.
NEVER_PACKAGE = frozenset({"manualsync_launcher"})


def _local_modules() -> set[str]:
    return {p.stem for p in BASE.glob("*.py")}


def runtime_modules() -> list[str]:
    """Every local module reachable from an entry point, as file names.

    A closure, not a list: adding an import to app.py is enough to make
    the new module ship. Test suites are excluded by construction --
    nothing an entry point imports is a regressione_* module, and if one
    ever were, that would be the bug worth failing on.
    """
    local = _local_modules()
    seen: set[str] = set()
    stack = list(ENTRY_POINTS)
    while stack:
        name = stack.pop()
        if name in seen or name not in local:
            continue
        seen.add(name)
        tree = ast.parse((BASE / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                stack.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                stack.append(node.module.split(".")[0])
    return sorted(f"{m}.py" for m in seen - NEVER_PACKAGE)


def runtime_data() -> list[str]:
    """Data entries, relative to BASE. Directories keep their trailing name."""
    out: list[str] = []
    for d in RUNTIME_DATA_DIRS:
        out.extend(sorted(
            p.relative_to(BASE).as_posix() for p in (BASE / d).rglob("*") if p.is_file()))
    out.extend(RUNTIME_DATA_FILES)
    return sorted(out)


def payload_entries() -> list[str]:
    return sorted(set(runtime_modules()) | set(runtime_data()))


def missing_sources() -> list[str]:
    """Declared entries that do not exist in this checkout."""
    return [rel for rel in payload_entries() if not (BASE / rel).exists()]


def verify(app_dir: str | Path) -> list[str]:
    """Entries absent from a materialised payload directory."""
    app_dir = Path(app_dir)
    return [rel for rel in payload_entries() if not (app_dir / rel).exists()]


def _explain() -> list[str]:
    lines = [f"entry points: {', '.join(ENTRY_POINTS)}"]
    mods = runtime_modules()
    data = runtime_data()
    lines.append(f"modules (transitive import closure): {len(mods)}")
    lines.extend(f"  {m}" for m in mods)
    lines.append(f"data entries: {len(data)}")
    lines.extend(f"  {d}" for d in data)
    excluded = sorted(
        p.stem for p in BASE.glob("*.py")
        if not p.stem.startswith("regressione_")
        and f"{p.stem}.py" not in mods)
    lines.append(f"product modules NOT in the payload: {len(excluded)}")
    lines.extend(f"  {m}" for m in excluded)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--print", action="store_true", dest="do_print",
                       help="one payload path per line, for the build script")
    group.add_argument("--verify", metavar="APP_DIR",
                       help="check a materialised payload; exit 1 if incomplete")
    group.add_argument("--explain", action="store_true",
                       help="human-readable breakdown of what ships and why")
    args = parser.parse_args(argv)

    absent = missing_sources()
    if absent:
        print("PAYLOAD SOURCE MISSING FROM THIS CHECKOUT:", file=sys.stderr)
        for rel in absent:
            print(f"  {rel}", file=sys.stderr)
        return 1

    if args.do_print:
        for rel in payload_entries():
            print(rel)
        return 0

    if args.explain:
        print("\n".join(_explain()))
        return 0

    gaps = verify(args.verify)
    if gaps:
        print(f"PAYLOAD INCOMPLETE ({len(gaps)} missing) in {args.verify}:", file=sys.stderr)
        for rel in gaps:
            print(f"  {rel}", file=sys.stderr)
        return 1
    print(f"Payload completo: {len(payload_entries())} elementi verificati in {args.verify}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
