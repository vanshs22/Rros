#!/usr/bin/env python3
"""Fail-closed release gate for the RROS 73-point build.

The gate verifies the cheap local invariants and runs the complete available
Python suite. PostgreSQL/Docker/browser gates remain explicit failures until the
corresponding environment is supplied, rather than being silently skipped.
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    "app/main.py", "app/models.py", "app/services.py", "app/safety.py",
    "app/compliance.py", "app/review_system.py", "migrations/017_business_policies_and_review_analysis.sql",
    "migrations/018_rbac_agent_analyst_roles.sql", "migrations/019_attribution_types_and_indexes.sql", "migrations/020_billing_checkout.sql", "migrations/021_attribution_ledger_uniqueness.sql", "frontend/package.json", "frontend/package-lock.json", "scripts/ai_eval.py", "scripts/docker_smoke.py", "scripts/postgres_migration_smoke.py", "tests/fixtures/agent_eval.json", "frontend/e2e/customer-journey.mjs", "docs/RROS_PLAN.md", "docs/PLAN.md",
)


def run(command: list[str]) -> tuple[bool, str]:
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, output[-4000:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="run only checks supported by this environment")
    args = parser.parse_args()
    checks: list[dict[str, object]] = []
    for name in REQUIRED_FILES:
        checks.append({"name": f"required:{name}", "passed": (ROOT / name).is_file()})

    migration_names = sorted(p.name for p in (ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
    checks.append({"name": "forward_only_migrations", "passed": migration_names[-1].startswith("021_") and len(migration_names) >= 21})

    compile_ok, compile_output = run([sys.executable, "-m", "compileall", "-q", "app", "tests"])
    checks.append({"name": "python_compile", "passed": compile_ok, "output": compile_output})

    try:
        ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8"))
        import_ok = True
        import_output = ""
    except Exception as exc:
        import_ok = False
        import_output = repr(exc)
    checks.append({"name": "main_ast", "passed": import_ok, "output": import_output})

    tests_ok, test_output = run([sys.executable, "-m", "pytest", "-q"])
    checks.append({"name": "python_tests", "passed": tests_ok, "output": test_output})

    lint_ok, lint_output = run(["ruff", "check", "app", "scripts", "tests", "--select", "E9,F63,F7,F82"])
    checks.append({"name": "focused_lint", "passed": lint_ok, "output": lint_output})

    frontend_ok, frontend_output = run(["npm", "run", "build", "--prefix", "frontend"])
    checks.append({"name": "frontend_build", "passed": frontend_ok, "output": frontend_output})
    browser_ok, browser_output = run(["npm", "run", "test:e2e", "--prefix", "frontend"])
    checks.append({"name": "browser_customer_journey", "passed": browser_ok, "output": browser_output})

    docker_ok, docker_output = (run([sys.executable, "scripts/docker_smoke.py", "--tag", "rros-release-gate:local"]) if shutil.which("docker") else (False, "docker CLI unavailable"))
    checks.append({"name": "production_docker_smoke", "passed": docker_ok, "output": docker_output})
    postgres_ok, postgres_output = (run([sys.executable, "scripts/postgres_migration_smoke.py"]) if shutil.which("docker") else (False, "docker CLI unavailable"))
    checks.append({"name": "postgres_migration_from_zero", "passed": postgres_ok, "output": postgres_output})

    report = {"passed": all(bool(x["passed"]) for x in checks), "checks": checks}
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
