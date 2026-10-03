#!/usr/bin/env python3
"""Production RROS process supervisor: scheduler + durable workers in one container."""
from __future__ import annotations
import asyncio, logging, os
from app.scheduler import scheduler_loop
from app.worker import run_workers

async def main():
    workers = max(1, min(64, int(os.getenv("RROS_WORKERS", "8"))))
    interval = max(10, int(os.getenv("RROS_SCHEDULER_INTERVAL_SECONDS", "60")))
    await asyncio.gather(scheduler_loop(interval), run_workers(workers))

if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.run(main())
