"""Internal scheduler entry point; ownership and preferences are re-read by the sender."""

import asyncio
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.tasks import runTasks


if __name__ == "__main__":
    try:
        status = asyncio.run(runTasks())
        raise SystemExit(75 if status == "skipped_task_in_progress" else 0)
    except Exception as exc:
        logging.error("Personal scheduled job failed: %s", type(exc).__name__)
        raise SystemExit(1)
