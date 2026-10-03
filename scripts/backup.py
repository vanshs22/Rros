#!/usr/bin/env python3
"""Create a non-destructive PostgreSQL backup using pg_dump."""
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not args.database_url or not args.database_url.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
        raise SystemExit("A PostgreSQL DATABASE_URL is required")
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--file", str(output), args.database_url])
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    print(f"backup created: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
