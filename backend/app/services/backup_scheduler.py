from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlmodel import Session, col, select

from app.core.database import engine
from app.models.backup import BackupSchedule, BackupTrigger
from app.services.backup_coordinator import BackupCoordinator

logger = logging.getLogger(__name__)


class BackupRunner(Protocol):
    async def run_backup_for_user(self, user_id: int, trigger: BackupTrigger) -> object: ...


class BackupScheduler:
    _CLAIM_DURATION = timedelta(minutes=5)

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
        coordinator: BackupRunner | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval_seconds: float = 60.0,
        close_sessions: bool = True,
    ) -> None:
        self.session_factory = session_factory or (lambda: Session(engine))
        self.coordinator = coordinator or BackupCoordinator()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.poll_interval_seconds = max(1.0, min(poll_interval_seconds, 300.0))
        self.close_sessions = close_sessions
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run_loop(), name="backup-scheduler")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def run_due_once(self, now: datetime) -> int:
        user_ids = self._claim_due_user_ids(now)
        for user_id in user_ids:
            try:
                await self.coordinator.run_backup_for_user(user_id, BackupTrigger.scheduled)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Scheduled backup did not complete for user_id=%s", user_id)
        return len(user_ids)

    async def _run_loop(self) -> None:
        try:
            while True:
                try:
                    await self.run_due_once(self.clock())
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error("Backup scheduler tick did not complete")
                await self.sleep(self.poll_interval_seconds)
        except asyncio.CancelledError:
            return

    def _claim_due_user_ids(self, now: datetime) -> list[int]:
        with self._session() as session:
            schedules = session.exec(
                select(BackupSchedule)
                .where(
                    col(BackupSchedule.enabled).is_(True),
                    col(BackupSchedule.next_due_at).is_not(None),
                    col(BackupSchedule.next_due_at) <= now,
                )
                .with_for_update(skip_locked=True)
            ).all()
            user_ids = [schedule.user_id for schedule in schedules]
            for schedule in schedules:
                schedule.next_due_at = now + self._CLAIM_DURATION
                session.add(schedule)
            session.commit()
            return user_ids

    @contextmanager
    def _session(self) -> Generator[Session, None, None]:
        session = self.session_factory()
        try:
            yield session
        finally:
            if self.close_sessions:
                session.close()
