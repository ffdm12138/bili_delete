#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pack all git-tracked files into a zip for sync.

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


def _run(cmd: list[str]) -> str:
    """Run a git command and return stdout, or empty string on failure."""
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    return r.stdout if r.returncode == 0 else ""


def get_tracked_files() -> list[str]:
    """Return list of git-tracked file paths."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True, text=False, cwd=str(ROOT),
    )
    if result.returncode != 0:
        print("[ERR] git ls-files failed — is this a git repository?", file=sys.stderr)
        sys.exit(1)
    raw = result.stdout.decode("utf-8", errors="replace")
    return [f for f in raw.split("\0") if f]


def pack_zip() -> Path:
    """Create zip archive, overwriting existing."""
    files = get_tracked_files()
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


def pack_snapshot() -> Path:
    """Generate PROJECT_SNAPSHOT_YYYYmmdd_HHMM.md."""
    ts = datetime.now(TZ_SHANGHAI).strftime("%Y%m%d_%H%M")
    name = f"PROJECT_SNAPSHOT_{ts}.md"
    output_path = ROOT / name

    commit = _run(["git", "rev-parse", "HEAD"]).strip()
    status = _run(["git", "status", "--short"])
    diff = _run(["git", "diff", "--stat", "HEAD~1..HEAD"])
    files = get_tracked_files()

    text_files = [f for f in files if f.endswith((".py", ".md", ".txt", ".gitignore"))]

    lines = [
        f"# Project Snapshot — {ts}",
        "",
        f"**Commit:** `{commit[:12]}`",
        "",
        "## git status",
        "```",
        status or "(clean)",
        "```",
        "",
        "## git diff --stat HEAD~1..HEAD",
        "```",
        diff or "(no parent commit)",
        "```",
        "",
        "## Tracked files",
        "```",
        *files,
        "```",
    ]

    for rel in sorted(text_files):
        src = ROOT / rel
        try:
            content = src.read_text(encoding="utf-8")
        except Exception:
            content = "(binary or unreadable)"
        lines += [
            "",
            f"## {rel}",
            "```python" if rel.endswith(".py") else "```",
            content,
            "```",
        ]

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[OK] {name}  — {len(files)} tracked files listed")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="打包 git 跟踪文件")
    parser.add_argument(
        "--snapshot", action="store_true",
        help="同时生成 PROJECT_SNAPSHOT_*.md（含全部文本文件内容）",
    )
    args = parser.parse_args()

    pack_zip()
    if args.snapshot:
        pack_snapshot()


if __name__ == "__main__":
    main()
