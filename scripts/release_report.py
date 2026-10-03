"""Emit a machine-readable local definition-of-done/release report."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETED_TESTS = ["tests/test_release_readiness.py"]


def run_targeted_tests() -> tuple[bool, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *TARGETED_TESTS],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, output[-4000:]


def run_frontend_build() -> tuple[bool, str]:
    completed = subprocess.run(
        ["npm", "run", "build", "--prefix", "frontend"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0, (completed.stdout + completed.stderr).strip()[-4000:]


def run_docker_smoke() -> tuple[bool, str]:
    completed = subprocess.run(
        [sys.executable, "scripts/docker_smoke.py", "--tag", "rros-release-smoke:local"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0, (completed.stdout + completed.stderr).strip()[-4000:]


def run_postgres_smoke() -> tuple[bool, str]:
    completed = subprocess.run(
        [sys.executable, "scripts/postgres_migration_smoke.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0, (completed.stdout + completed.stderr).strip()[-4000:]


def run_browser_journey() -> tuple[bool, str]:
    completed = subprocess.run(
        ["npm", "run", "test:e2e", "--prefix", "frontend"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0, (completed.stdout + completed.stderr).strip()[-4000:]


def build_report() -> dict:
    tests_passed, test_output = run_targeted_tests()
    frontend_passed, frontend_output = run_frontend_build()
    browser_passed, browser_output = run_browser_journey()
    docker_available = bool(shutil.which("docker"))
    docker_passed, docker_output = run_docker_smoke() if docker_available else (False, "docker CLI unavailable")
    postgres_passed, postgres_output = run_postgres_smoke() if docker_available else (False, "docker CLI unavailable")
    # These are deliberately not inferred from installed CLIs: availability is not
    # equivalent to a successful production migration, browser, or Docker smoke test.
    external = {
        "postgresql_migration_from_zero": {
            "passed": postgres_passed,
            "available": docker_available,
            "output": postgres_output,
            "reason": "disposable PostgreSQL migration smoke" if postgres_passed else "PostgreSQL migration smoke failed or Docker unavailable",
        },
        "browser_customer_journey": {
            "passed": browser_passed,
            "available": True,
            "output": browser_output,
            "reason": "Playwright journey passed" if browser_passed else "Playwright journey failed",
        },
        "production_docker_smoke": {
            "passed": docker_passed,
            "available": docker_available,
            "output": docker_output,
            "reason": "image build and bundled-code smoke" if docker_passed else "Docker smoke failed or CLI unavailable",
        },
    }
    local_checks = {
        "targeted_readiness_tests": {"passed": tests_passed, "output": test_output},
        "frontend_build": {"passed": frontend_passed, "output": frontend_output},
        "integrity_scanner_present": {"passed": (ROOT / "scripts/integrity_check.py").is_file()},
        "readiness_tests_present": {"passed": (ROOT / "tests/test_release_readiness.py").is_file()},
    }
    return {
        "report": "rros-definition-of-done",
        "scope": "checklist points 47-52 and 69-73",
        "local_ready": all(item["passed"] for item in local_checks.values()),
        "release_ready": all(item["passed"] for item in local_checks.values()) and all(item["passed"] for item in external.values()),
        "local_checks": local_checks,
        "external_checks": external,
        "claims": {
            "external_postgresql_docker_browser_verified": False,
            "note": "Unavailable external checks remain failed rather than being silently treated as passed.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="also write JSON to this path")
    args = parser.parse_args()
    report = build_report()
    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        Path(args.output).write_text(encoded + "\n", encoding="utf-8")
    # CI can certify local readiness while the report still correctly says that the
    # complete release definition is not met without external environments.
    return 0 if report["local_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
