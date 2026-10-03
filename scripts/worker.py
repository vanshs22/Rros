from app.worker import run_workers
import asyncio
import os

asyncio.run(run_workers(int(os.getenv("RROS_WORKERS", "4"))))
