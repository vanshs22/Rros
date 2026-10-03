#!/usr/bin/env python3
"""Run the deterministic Agent OS evaluation/regression dataset."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", "tests/test_ai_evaluation.py"], cwd=ROOT))
