#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pack all git-tracked files into a zip for sync.

Usage:
    python pack.py              # output: bili_delete.zip (overwrite)
"""

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT_NAME = "bili_delete.zip"


def get_tracked_files() -> list[str]:
    """Return list of git-tracked file paths."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=False,
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        print("[ERR] git ls-files failed — is this a git repository?", file=sys.stderr)
        sys.exit(1)

    raw = result.stdout.decode("utf-8", errors="replace")
    return [f for f in raw.split("\0") if f]


def pack() -> Path:
    """Create zip archive of all git-tracked files, overwriting existing."""
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


if __name__ == "__main__":
    pack()
