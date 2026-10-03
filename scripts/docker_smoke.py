#!/usr/bin/env python3
"""Build the production image and verify its bundled Python/React artifacts."""
from __future__ import annotations

import argparse
import subprocess
import sys
import time


def run(command: list[str]) -> None:
    result = subprocess.run(command, text=True)
    if result.returncode:
        raise SystemExit(result.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=f"rros-smoke:{int(time.time())}")
    args = parser.parse_args()
    run(["docker", "build", "-t", args.tag, "."])
    run(["docker", "run", "--rm", "--entrypoint", "python", args.tag, "-m", "compileall", "-q", "/app/app", "/app/scripts"])
    print({"passed": True, "tag": args.tag, "checks": ["image_build", "bundled_python_compile"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
