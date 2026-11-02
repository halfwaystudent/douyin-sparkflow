"""Start one claimed personal job at a time alongside the legacy crontab."""

import os
from pathlib import Path
import subprocess
import sys

from core.personal_streak import claim_due, finish_claim, renew_claim


class PersonalDispatcher:
    def __init__(self):
        self.process = None
        self.job = None

    def tick(self):
        if self.process is not None:
            code = self.process.poll()
            if code is None:
                renew_claim(self.job)
                return
            finish_claim(self.job, busy=code == 75)
            self.process = self.job = None
        job = claim_due()
        if not job:
            return
        environment = dict(os.environ)
        for key in ("SPARKFLOW_MANUAL_RUN", "SPARKFLOW_MANUAL_FORCE_ALL", "SPARKFLOW_MANUAL_FAILED_ONLY", "SPARKFLOW_MANUAL_UNSENT_ONLY", "SPARKFLOW_FALLBACK_PHASE", "SPARKFLOW_ACCOUNT_REFS", "SPARKFLOW_TASK_MODE"):
            environment.pop(key, None)
        environment.update(
            SPARKFLOW_PERSONAL_USER_ID=job["key"], SPARKFLOW_PERSONAL_REVISION=job["revision"],
            SPARKFLOW_PERSONAL_CLAIM=job["token"], SPARKFLOW_TRIGGER_LABEL="personal scheduled send",
        )
        script = Path(__file__).resolve().parents[1] / "scripts/personal_streak_job.py"
        try:
            self.process = subprocess.Popen([sys.executable, str(script)], cwd=str(script.parent.parent), env=environment)
            self.job = job
        except OSError:
            finish_claim(job, busy=True)
            raise
