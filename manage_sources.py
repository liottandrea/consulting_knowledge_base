"""Manage sources.json — the list of source directories the pipeline ingests.

sources.json is machine-specific (gitignored); paths differ per machine (a
OneDrive mount, a local inbox, a NAS share). This CLI edits it safely and, most
usefully, verifies that configured directories resolve and actually contain
supported documents before you run the pipeline.

    uv run python manage_sources.py list
    uv run python manage_sources.py add           # interactive
    uv run python manage_sources.py add --name "OneDrive Main" --path "/Users/..." --recurse
    uv run python manage_sources.py remove "OneDrive Main"
    uv run python manage_sources.py check          # verify dirs + count files

`check` is the one to run after adding a source: it confirms the path resolves,
is readable, and reports a count of .pptx/.pdf/.docx files per source (recursing
when the entry is configured to).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ingest import SUPPORTED_EXT, _is_ignorable
from kb_common import ROOT, SOURCES_CONFIG, load_source_dirs


def _read_raw() -> list[dict]:
    """Read sources.json verbatim (not the resolved view from load_source_dirs).

    Editing operations preserve the on-disk relative paths the user typed.
    """
    if not SOURCES_CONFIG.exists():
        return []
    try:
        data = json.loads(SOURCES_CONFIG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  ✗ {SOURCES_CONFIG.name} is unreadable: {exc}", file=sys.stderr)
        raise SystemExit(2)
    return data if isinstance(data, list) else []


def _write_raw(entries: list[dict]) -> None:
    SOURCES_CONFIG.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (ROOT / p).resolve()


def _count_files(root: Path, recurse: bool) -> int:
    if not root.exists():
        return 0
    walker = root.rglob("*") if recurse else root.iterdir()
    return sum(
        1 for p in walker
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT and not _is_ignorable(p.name)
    )


def cmd_list(_args) -> int:
    entries = _read_raw()
    if not entries:
        print(f"No sources configured ({SOURCES_CONFIG.name} missing or empty).")
        print("Falling back to ROOT/sources. Copy sources.example.json to get started.")
        return 0
    print(f"{len(entries)} source dir(s) in {SOURCES_CONFIG.name}:")
    for e in entries:
        rec = "recursive" if e.get("recurse") else "top-level"
        print(f"  • {e.get('name')}  [{rec}]  {e.get('path')}")
    return 0


def cmd_add(args) -> int:
    name = args.name or input("Name (label, e.g. 'OneDrive Main'): ").strip()
    path = args.path or input("Path (absolute or relative to repo): ").strip()
    if args.recurse:
        recurse = True
    elif args.name or args.path:
        recurse = False  # non-interactive: default off unless --recurse given
    else:
        recurse = input("Recurse into subdirectories? [y/N]: ").strip().lower() == "y"
    if not name or not path:
        print("  ✗ name and path are required.", file=sys.stderr)
        return 2

    entries = _read_raw()
    if any(e.get("name") == name for e in entries):
        print(f"  ✗ a source named '{name}' already exists.", file=sys.stderr)
        return 2
    entries.append({"name": name, "path": path, "recurse": recurse})
    _write_raw(entries)
    print(f"  ✓ added '{name}' -> {path} ({'recursive' if recurse else 'top-level'})")
    print("  Run `manage_sources.py check` to verify it resolves.")
    return 0


def cmd_remove(args) -> int:
    entries = _read_raw()
    kept = [e for e in entries if e.get("name") != args.name]
    if len(kept) == len(entries):
        print(f"  ✗ no source named '{args.name}'.", file=sys.stderr)
        return 2
    _write_raw(kept)
    print(f"  ✓ removed '{args.name}'")
    return 0


def cmd_check(_args) -> int:
    entries = load_source_dirs()  # resolved view (fallback applied if no config)
    print(f"Checking {len(entries)} source dir(s):\n")
    ok = True
    for e in entries:
        root = Path(e["path"])
        if not root.exists():
            print(f"  ✗ {e['name']}: NOT FOUND -> {root}")
            ok = False
            continue
        if not root.is_dir():
            print(f"  ✗ {e['name']}: not a directory -> {root}")
            ok = False
            continue
        try:
            n = _count_files(root, bool(e.get("recurse")))
        except PermissionError:
            print(f"  ✗ {e['name']}: not readable -> {root}")
            ok = False
            continue
        rec = "recursive" if e.get("recurse") else "top-level"
        flag = "" if n else "  ⚠ no supported files found"
        print(f"  ✓ {e['name']} [{rec}]: {n} file(s) -> {root}{flag}")
    print()
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Manage sources.json")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show configured source dirs")
    p_add = sub.add_parser("add", help="add a source dir (interactive if no flags)")
    p_add.add_argument("--name")
    p_add.add_argument("--path")
    p_add.add_argument("--recurse", action="store_true")
    p_rm = sub.add_parser("remove", help="remove a source dir by name")
    p_rm.add_argument("name")
    sub.add_parser("check", help="verify configured dirs exist and count files")

    args = parser.parse_args(argv)
    return {
        "list": cmd_list, "add": cmd_add, "remove": cmd_remove, "check": cmd_check,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
