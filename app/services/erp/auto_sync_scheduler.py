# @file    services/erp/auto_sync_scheduler.py
#
# Lightweight APScheduler-based runner for the Business Central auto-sync.
# We intentionally keep the scheduler **single-instance, in-process**:
# uvicorn workers > 1 should set BC_AUTO_SYNC_ENABLED on exactly one
# instance to avoid duplicate cron firings. The orchestrator's per-entity
# lock makes accidental duplicates safe — they short-circuit with
# SYNC_ALREADY_RUNNING — but a single scheduler is still preferable.

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.erp.auto_sync_service import (
    AutoSyncTickResult, run_auto_sync_tick,
)


_log = logging.getLogger(__name__)


def _parse_cron_expression(expr: str) -> dict[str, str]:
    """Parse a 5-field cron expression into APScheduler kwargs.
    Falls back to ``0 2 * * *`` (daily 02:00) on bad input rather than
    raising at startup."""
    parts = (expr or "").strip().split()
    if len(parts) != 5:
        _log.warning(
            "BC_AUTO_SYNC_CRON %r is not a 5-field expression; "
            "falling back to '0 2 * * *'.", expr,
        )
        parts = ["0", "2", "*", "*", "*"]
    return {
        "minute":       parts[0],
        "hour":         parts[1],
        "day":          parts[2],
        "month":        parts[3],
        "day_of_week":  parts[4],
    }


class BcAutoSyncScheduler:
    """Wraps APScheduler so the rest of the codebase doesn't import the
    third-party module directly. Provides ``start`` / ``stop`` /
    ``run_now`` and a small status payload for the admin endpoint.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scheduler = None  # apscheduler.schedulers.background.BackgroundScheduler
        self._job = None
        self._last_tick: Optional[AutoSyncTickResult] = None
        self._timezone_str: str = settings.BC_AUTO_SYNC_TIMEZONE
        self._cron_str: str = settings.BC_AUTO_SYNC_CRON

    # ── Public API ────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return bool(settings.BC_AUTO_SYNC_ENABLED)

    @property
    def is_running(self) -> bool:
        return bool(self._scheduler and self._scheduler.running)

    @property
    def cron(self) -> str:
        return self._cron_str

    @property
    def timezone(self) -> str:
        return self._timezone_str

    @property
    def last_tick(self) -> Optional[AutoSyncTickResult]:
        return self._last_tick

    def next_run_at(self) -> Optional[datetime]:
        if not self.is_running or self._job is None:
            return None
        return getattr(self._job, "next_run_time", None)

    def start(self) -> None:
        """Idempotent. No-op when ``BC_AUTO_SYNC_ENABLED=False`` or the
        scheduler is already running. Failures are logged and swallowed
        so a misconfigured scheduler never blocks the API process from
        booting."""
        if not self.enabled:
            _log.info("BC auto-sync scheduler disabled (BC_AUTO_SYNC_ENABLED=false).")
            return
        with self._lock:
            if self._scheduler is not None and self._scheduler.running:
                return
            try:
                self._scheduler, self._job = self._build_scheduler()
                self._scheduler.start()
                next_at = getattr(self._job, "next_run_time", None)
                _log.info(
                    "BC auto-sync scheduler started (cron=%r tz=%s); "
                    "next run at %s.",
                    self._cron_str, self._timezone_str, next_at,
                )
            except Exception:
                _log.exception("BC auto-sync scheduler failed to start.")
                self._scheduler = None
                self._job = None

    def stop(self) -> None:
        """Idempotent shutdown. Always safe to call."""
        with self._lock:
            if self._scheduler is None:
                return
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:  # pragma: no cover - defensive
                _log.exception("BC auto-sync scheduler shutdown failed.")
            self._scheduler = None
            self._job = None

    def run_now(
        self, *, target_entity_id: Optional[str] = None,
        force: bool = False, actor_id: Optional[int] = None,
    ) -> AutoSyncTickResult:
        """Manual tick — invoked by the admin run-now endpoint. Always
        opens its own session so it works even when the scheduler is off."""
        result = self._invoke_tick(
            target_entity_id=target_entity_id, force=force, actor_id=actor_id,
        )
        return result

    def status(self) -> dict[str, object]:
        """Snapshot used by the admin status endpoint."""
        last = self._last_tick
        return {
            "enabled":         self.enabled,
            "cron":            self._cron_str,
            "timezone":        self._timezone_str,
            "scheduler_running": self.is_running,
            "next_run_at":     self.next_run_at(),
            "last_started_at":  last.started_at if last else None,
            "last_finished_at": last.finished_at if last else None,
            "last_status": (
                "success" if last and all(r.ok or r.skipped for r in last.results)
                else "failed" if last
                else None
            ),
            "last_eligible_entity_ids": last.eligible_entity_ids if last else [],
            "last_results_summary": (
                [{
                    "entity_id":     r.entity_id,
                    "ok":            r.ok,
                    "skipped":       r.skipped,
                    "skip_reason":   r.skip_reason,
                    "safe_error_code":    r.safe_error_code,
                    "safe_error_message": r.safe_error_message,
                    "step_summary":  r.step_summary(),
                } for r in last.results] if last else []
            ),
        }

    # ── Internals ─────────────────────────────────────────────────────
    def _build_scheduler(self):
        # Imports kept local so unit tests that don't need APScheduler
        # can import this module without the dependency at test time.
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = BackgroundScheduler(daemon=True, timezone=self._timezone_str)
        cron_kwargs = _parse_cron_expression(self._cron_str)
        try:
            trigger = CronTrigger(timezone=self._timezone_str, **cron_kwargs)
        except Exception:
            # Bad timezone — fall back to UTC rather than crash startup.
            _log.warning(
                "BC_AUTO_SYNC_TIMEZONE %r is invalid; falling back to UTC.",
                self._timezone_str,
            )
            self._timezone_str = "UTC"
            trigger = CronTrigger(timezone="UTC", **cron_kwargs)

        job = scheduler.add_job(
            self._scheduled_tick, trigger=trigger,
            id="bc_auto_sync_tick", replace_existing=True,
            coalesce=True, max_instances=1, misfire_grace_time=300,
            name="Business Central auto-sync",
        )
        return scheduler, job

    def _scheduled_tick(self) -> None:
        """APScheduler thread entry-point. Always swallows exceptions —
        a crashing tick must not poison the scheduler thread."""
        try:
            self._invoke_tick()
        except Exception:  # pragma: no cover - defensive
            _log.exception("BC auto-sync tick crashed.")

    def _invoke_tick(
        self, *, target_entity_id: Optional[str] = None,
        force: bool = False, actor_id: Optional[int] = None,
    ) -> AutoSyncTickResult:
        db = SessionLocal()
        try:
            result = run_auto_sync_tick(
                db, target_entity_id=target_entity_id, force=force,
                actor_id=actor_id,
            )
            self._last_tick = result
            return result
        finally:
            db.close()


# Module-level singleton used by the FastAPI lifespan + admin router.
bc_auto_sync_scheduler = BcAutoSyncScheduler()
