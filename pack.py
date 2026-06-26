#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pack all project files into a zip for sync.

Usage:
    python pack.py               # output: bili_delete.zip (overwrite)
    python pack.py --snapshot    # also generate PROJECT_SNAPSHOT_*.md
"""

import argparse
import subprocess
import sys
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT_NAME = "bili_delete.zip"
TZ_SHANGHAI = timezone(timedelta(hours=8))

# Patterns excluded in filesystem fallback mode (fnmatch-style prefix/suffix)
FS_EXCLUDE = {
    ".git", "__pycache__", ".pytest_cache",
    ".venv", "venv",
}
FS_EXCLUDE_SUFFIX = {".pyc", ".zip", ".db", ".sqlite"}
FS_EXCLUDE_PREFIX = {"debug_", "candidates_", "PROJECT_SNAPSHOT_"}


def _run(cmd: list[str]) -> str:
    """Run a git command and return stdout, or empty string on failure."""
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    return r.stdout if r.returncode == 0 else ""


def _is_git_repo() -> bool:
    return (ROOT / ".git").exists()


def get_tracked_files() -> list[str]:
    """Return list of project files.  Uses git ls-files if available,
    otherwise falls back to filesystem scan with sensible excludes."""
    if _is_git_repo():
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            capture_output=True, text=False, cwd=str(ROOT),
        )
        if result.returncode == 0:
            raw = result.stdout.decode("utf-8", errors="replace")
            return [f for f in raw.split("\0") if f]

    # Fallback: walk the directory tree
    print("[WARN] not a git repository; using filesystem fallback")
    files = []
    for p in ROOT.rglob("*"):
        if p.is_dir():
            continue
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        parts = rel.split("/")
        if any(part in FS_EXCLUDE for part in parts):
            continue
        if any(part.endswith(s) for s in FS_EXCLUDE_SUFFIX for part in parts):
            continue
        if any(part.startswith(s) for s in FS_EXCLUDE_PREFIX for part in parts):
            continue
        files.append(rel)
    files.sort()
    return files


def pack_zip(files: list[str]) -> Path:
    """Create zip archive, overwriting existing."""
    output_path = ROOT / OUTPUT_NAME
    written = 0
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            src = ROOT / rel
            if not src.is_file():
                continue
            zf.write(src, rel)
            written += 1
    size_kb = output_path.stat().st_size / 1024
    print(f"[OK] {OUTPUT_NAME}  — {written} files, {size_kb:.1f} KB")
    return output_path


def pack_snapshot(files: list[str]) -> Path:
    """Generate PROJECT_SNAPSHOT_YYYYmmdd_HHMM.md.
    Uses 4-backtick fences to avoid nesting issues with Markdown content."""
    ts = datetime.now(TZ_SHANGHAI).strftime("%Y%m%d_%H%M")
    name = f"PROJECT_SNAPSHOT_{ts}.md"
    output_path = ROOT / name

    commit = _run(["git", "rev-parse", "HEAD"]).strip() if _is_git_repo() else "N/A"
    status = _run(["git", "status", "--short"]) if _is_git_repo() else ""
    diff = _run(["git", "diff", "--stat", "HEAD~1..HEAD"]) if _is_git_repo() else ""

    text_files = sorted(
        f for f in files if f.endswith((".py", ".md", ".txt", ".gitignore"))
    )

    lines = [
        f"# Project Snapshot — {ts}",
        "",
        f"**Commit:** `{commit[:40] if commit else 'N/A'}`",
        "",
        "## git status",
        "````text",
        status or "(clean / not a git repo)",
        "````",
        "",
        "## git diff --stat HEAD~1..HEAD",
        "````text",
        diff or "(no parent commit / not a git repo)",
        "````",
        "",
        "## Tracked files",
        "````text",
        *files,
        "````",
    ]

    for rel in text_files:
        src = ROOT / rel
        try:
            content = src.read_text(encoding="utf-8")
        except Exception:
            content = "(binary or unreadable)"
        lines += [
            "",
            f"## {rel}",
            "````text",
            content,
            "````",
        ]

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[OK] {name}  — {len(files)} tracked files listed")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="打包项目文件为 zip")
    parser.add_argument(
        "--snapshot", action="store_true",
        help="同时生成 PROJECT_SNAPSHOT_*.md（含全部文本文件内容）",
    )
    args = parser.parse_args()

    files = get_tracked_files()
    pack_zip(files)
    if args.snapshot:
        pack_snapshot(files)


if __name__ == "__main__":
    main()
