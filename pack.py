#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pack all git-tracked files into a zip for sync.

Usage:
    python pack.py              # output: bili_delete_YYYYmmdd_HHMM.zip
    python pack.py --name foo   # output: foo.zip
"""

import argparse
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

TZ_SHANGHAI = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent


def get_tracked_files() -> list[str]:
    """Return list of git-tracked file paths (not deleted, not ignored)."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=False,
        cwd=str(ROOT),
    )
    # --cached: tracked files
    # We only want tracked (committed) files, so use plain ls-files
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
    files = [f for f in raw.split("\0") if f]
    return files


def pack(output_name: str) -> Path:
    """Create zip archive of all git-tracked files."""
    files = get_tracked_files()
    output_path = ROOT / output_name

    written = 0
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            src = ROOT / rel
            if not src.is_file():
                continue
            zf.write(src, rel)
            written += 1

    size_kb = output_path.stat().st_size / 1024
    print(f"[OK] {output_name}  — {written} files, {size_kb:.1f} KB")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="打包 git 跟踪文件为 zip")
    parser.add_argument(
        "--name",
        help="输出文件名（不含路径），默认自动生成时间戳名称",
    )
    args = parser.parse_args()

    if args.name:
        name = args.name
        if not name.endswith(".zip"):
            name += ".zip"
    else:
        ts = datetime.now(TZ_SHANGHAI).strftime("%Y%m%d_%H%M")
        name = f"bili_delete_{ts}.zip"

    pack(name)


if __name__ == "__main__":
    main()
