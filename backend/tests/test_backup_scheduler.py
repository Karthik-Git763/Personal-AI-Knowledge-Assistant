from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from app.models.backup import BackupSchedule, BackupTrigger
from app.models.user import User
from app.services.backup_scheduler import BackupScheduler


class SpyCoordinator:
    def __init__(self, failures: set[int] | None = None) -> None:
        self.failures = failures or set()
        self.started_user_ids: list[int] = []

    async def run_backup_for_user(self, user_id: int, trigger: BackupTrigger) -> object:
        self.started_user_ids.append(user_id)
        if user_id in self.failures:
            raise RuntimeError("configured failure")
        return object()


def _due_schedule(session: Session, email: str, due_at: datetime) -> BackupSchedule:
    user = User(email=email, hashed_password="hashed-password")
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id is not None
    schedule = BackupSchedule(user_id=user.id, enabled=True, next_due_at=due_at)
    session.add(schedule)
    session.commit()
    return schedule


@pytest.mark.asyncio
async def test_scheduler_runs_overdue_schedule_once(session: Session) -> None:
    overdue_schedule = _due_schedule(session, "overdue@example.com", datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
    coordinator = SpyCoordinator()
    scheduler = BackupScheduler(
        session_factory=lambda: session, coordinator=coordinator, close_sessions=False
    )

    count = await scheduler.run_due_once(now=overdue_schedule.next_due_at + timedelta(minutes=1))  # type: ignore[operator]

    assert count == 1
    assert coordinator.started_user_ids == [overdue_schedule.user_id]


@pytest.mark.asyncio
async def test_scheduler_claim_prevents_duplicate_dispatch_across_workers(session: Session) -> None:
    due_at = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    _due_schedule(session, "claim@example.com", due_at)
    coordinator = SpyCoordinator()
    first_scheduler = BackupScheduler(
        session_factory=lambda: session, coordinator=coordinator, close_sessions=False
    )
    second_scheduler = BackupScheduler(
        session_factory=lambda: session, coordinator=coordinator, close_sessions=False
    )

    first = await first_scheduler.run_due_once(now=due_at + timedelta(minutes=1))
    second = await second_scheduler.run_due_once(now=due_at + timedelta(minutes=1))

    assert first == 1
    assert second == 0
    assert len(coordinator.started_user_ids) == 1


@pytest.mark.asyncio
async def test_scheduler_continues_after_one_user_failure(session: Session) -> None:
    due_at = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    failed = _due_schedule(session, "failed-schedule@example.com", due_at)
    later = _due_schedule(session, "later-schedule@example.com", due_at)
    assert failed.user_id is not None
    assert later.user_id is not None
    coordinator = SpyCoordinator(failures={failed.user_id})
    scheduler = BackupScheduler(
        session_factory=lambda: session, coordinator=coordinator, close_sessions=False
    )

    count = await scheduler.run_due_once(now=due_at + timedelta(minutes=1))

    assert count == 2
    assert coordinator.started_user_ids == [failed.user_id, later.user_id]


@pytest.mark.asyncio
async def test_scheduler_stop_cancels_idle_loop_without_waiting(session: Session) -> None:
    sleep_started = asyncio.Event()

    async def blocking_sleep(_: float) -> None:
        sleep_started.set()
        await asyncio.Event().wait()

    scheduler = BackupScheduler(
        session_factory=lambda: session,
        coordinator=SpyCoordinator(),
        sleep=blocking_sleep,
        poll_interval_seconds=60,
        close_sessions=False,
    )

    await scheduler.start()
    await asyncio.wait_for(sleep_started.wait(), timeout=1)
    await scheduler.stop()

    assert scheduler.running is False
