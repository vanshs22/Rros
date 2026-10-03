#!/usr/bin/env python3
"""Transactional PostgreSQL migration runner for Revenue Recovery OS."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"

def main() -> None:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith(("postgresql://", "postgres://")):
        raise SystemExit("DATABASE_URL must point to PostgreSQL for production migrations")
    files = sorted(p for p in MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql") if p.is_file())
    with psycopg.connect(url, autocommit=False) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rros_schema_migrations (
                version TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        applied = {r[0]: r[1] for r in conn.execute("SELECT version, checksum FROM rros_schema_migrations")}
        for path in files:
            version = path.name.split("_", 1)[0]
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    raise SystemExit(f"Migration checksum changed after application: {path.name}")
                print(f"skip {path.name}")
                continue
            print(f"apply {path.name}")
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO rros_schema_migrations(version, checksum) VALUES (%s, %s)", (version, checksum))
        conn.commit()
        print("migrations complete")

if __name__ == "__main__":
    main()
