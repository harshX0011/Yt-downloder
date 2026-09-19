"""In-process download job registry.

Jobs are intentionally not persisted: a finished file lives in a per-job
directory under `DOWNLOAD_DIR` and is deleted once `JOB_TTL_SECONDS` elapses or
the client releases it, whichever comes first. Restarting the process starts
from a clean slate.

Progress is written by whichever thread is doing the transfer (the event loop
for direct downloads, a worker thread for yt-dlp) and read by the HTTP layer.
Only whole-attribute assignments and an integer version bump cross that
boundary, so no lock is needed on the read path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .downloader import DownloadFailed, remove_tree
from .models import JobState

logger = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED})


@dataclass
class Job:
    """A single download, from request to expiry."""

    job_id: str
    url: str
    directory: Path
    state: JobState = JobState.QUEUED
    title: str | None = None
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed_bytes_per_second: float | None = None
    eta_seconds: int | None = None
    filename: str | None = None
    filesize_bytes: int | None = None
    path: Path | None = None
    error: str | None = None
    error_code: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    version: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def progress_percent(self) -> float:
        if self.state is JobState.COMPLETED:
            return 100.0
        if not self.total_bytes:
            return 0.0
        return round(min(100.0, self.downloaded_bytes / self.total_bytes * 100), 1)

    def expires_in_seconds(self, ttl: int) -> int | None:
        if self.finished_at is None:
            return None
        return max(0, int(self.finished_at + ttl - time.time()))

    def touch(self) -> None:
        self.version += 1

    def report(
        self,
        state: JobState,
        downloaded: int,
        total: int | None,
        speed: float | None,
        eta: int | None,
    ) -> None:
        """Progress callback handed to the downloader. May run off-loop."""
        self.state = state
        self.downloaded_bytes = downloaded
        if total:
            self.total_bytes = total
        self.speed_bytes_per_second = speed
        self.eta_seconds = eta
        self.touch()

    def fail(self, message: str, code: str) -> None:
        self.state = JobState.FAILED
        self.error = message
        self.error_code = code
        self.finished_at = time.time()
        self.speed_bytes_per_second = None
        self.eta_seconds = None
        self.touch()

    def complete(self, path: Path, filename: str, filesize: int) -> None:
        self.state = JobState.COMPLETED
        self.path = path
        self.filename = filename
        self.filesize_bytes = filesize
        self.downloaded_bytes = filesize
        self.total_bytes = self.total_bytes or filesize
        self.finished_at = time.time()
        self.speed_bytes_per_second = None
        self.eta_seconds = None
        self.touch()


class JobManager:
    """Creates, runs, exposes and reaps download jobs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._reaper: asyncio.Task | None = None

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop())

    async def shutdown(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None

        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

        for job in list(self._jobs.values()):
            remove_tree(job.directory)
        self._jobs.clear()

    # --- registry ----------------------------------------------------------

    def create(self, url: str, title: str | None = None) -> Job:
        job_id = secrets.token_urlsafe(16)
        directory = self._settings.download_dir / job_id
        directory.mkdir(parents=True, exist_ok=True)
        job = Job(job_id=job_id, url=url, directory=directory, title=title)
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    @property
    def active_count(self) -> int:
        return sum(1 for job in self._jobs.values() if not job.is_terminal)

    def run(self, job: Job, coroutine_factory) -> None:
        """Schedule `coroutine_factory()` as the job's worker."""
        task = asyncio.create_task(self._execute(job, coroutine_factory))
        self._tasks[job.job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job.job_id, None))

    async def _execute(self, job: Job, coroutine_factory) -> None:
        try:
            async with self._semaphore:
                if job.state is JobState.CANCELLED:
                    return
                job.state = JobState.PREPARING
                job.touch()
                outcome = await coroutine_factory()
                job.complete(outcome.path, outcome.filename, outcome.filesize_bytes)
        except asyncio.CancelledError:
            job.state = JobState.CANCELLED
            job.finished_at = time.time()
            job.touch()
            remove_tree(job.directory)
            raise
        except DownloadFailed as exc:
            job.fail(str(exc), exc.code)
            remove_tree(job.directory)
        except Exception:
            logger.exception("job %s failed unexpectedly", job.job_id)
            job.fail(
                "Something went wrong on the server while downloading. Please try again.",
                "internal_error",
            )
            remove_tree(job.directory)

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        else:
            job.state = JobState.CANCELLED
            job.finished_at = time.time()
            job.touch()
        remove_tree(job.directory)
        self._jobs.pop(job_id, None)
        return True

    # --- reaping -----------------------------------------------------------

    async def _reap_loop(self) -> None:
        interval = max(30, min(300, self._settings.job_ttl_seconds // 4 or 60))
        while True:
            try:
                await asyncio.sleep(interval)
                self.reap_expired()
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the reaper die
                logger.exception("job reaper iteration failed")

    def reap_expired(self) -> int:
        """Delete jobs whose TTL has elapsed. Returns how many were removed."""
        ttl = self._settings.job_ttl_seconds
        now = time.time()
        stale = [
            job_id
            for job_id, job in self._jobs.items()
            if job.finished_at is not None and job.finished_at + ttl <= now
        ]
        for job_id in stale:
            job = self._jobs.pop(job_id)
            remove_tree(job.directory)
        if stale:
            logger.info("reaped %d expired download job(s)", len(stale))
        return len(stale)
