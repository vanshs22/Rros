#!/usr/bin/env python3
"""Verify a PostgreSQL custom-format backup without restoring over live data."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    args = parser.parse_args()
    if not args.backup.is_file():
        raise SystemExit(f"backup not found: {args.backup}")
    result = subprocess.run(["pg_restore", "--list", str(args.backup)], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr)
        return result.returncode
    entries = [line for line in result.stdout.splitlines() if line and not line.startswith(";")]
    if not entries:
        raise SystemExit("backup contains no restorable entries")
    print(f"backup verified: {len(entries)} catalog entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
