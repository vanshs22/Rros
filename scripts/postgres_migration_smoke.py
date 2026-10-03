#!/usr/bin/env python3
"""Apply all numbered migrations to a disposable PostgreSQL container."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    command = [
        "docker", "run", "--rm", "--user", "postgres",
        "-v", f"{ROOT / 'migrations'}:/migrations:ro",
        "postgres:16-alpine", "sh", "-c",
        "set -eu; initdb -D /tmp/pgdata >/dev/null; "
        "pg_ctl -D /tmp/pgdata -o \"-c listen_addresses=127.0.0.1\" -w start >/dev/null; "
        "trap \"pg_ctl -D /tmp/pgdata -m fast stop >/dev/null\" EXIT; "
        "createdb rros; "
        "for file in /migrations/[0-9][0-9][0-9]_*.sql; do "
        "psql -v ON_ERROR_STOP=1 -d rros -f \"$file\" >/dev/null; "
        "done; echo postgres-migration-chain-passed",
    ]
    result = subprocess.run(command, cwd=ROOT, text=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
