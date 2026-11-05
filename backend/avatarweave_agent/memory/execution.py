"""Bounded lane admission, parent deadlines and physical-work accounting."""

import asyncio
from collections import Counter
from contextvars import ContextVar
from datetime import UTC, datetime
from time import monotonic

from sqlalchemy.exc import SQLAlchemyError

from .contracts import ErrorInfo, ExecutionReport, ModuleResult, ScopedRequest, StageTiming

_PARENT = ContextVar("memory_execution_parent", default=None)


class MemoryFault(Exception):
    def __init__(self, code, message, *, retryable=False):
        self.info = ErrorInfo(
            code=code,
            message=message,
            retryable=retryable,
            retry_after_ms=100 if retryable else None,
        )
        super().__init__(code)


class Executor:
    """No waiting queue: reject excess work; the SQL pool is separately bounded.

    Counters live in one event loop. Cross-process connection budgets are a
    deployment concern and must be included in the capacity manifest.
    """

    def __init__(self, max_running=8, per_user=2, clock=None):
        self.max_running = max_running
        self.per_user = per_user
        self.running = 0
        self.users = Counter()
        self.lanes = Counter()
        self.user_lanes = Counter()
        self.lane_limits = {"control": 2, "dialogue": 4, "deep_recall": 1, "maintenance": 1}
        self.user_lane_limits = {"control": 1, "dialogue": 2, "deep_recall": 1, "maintenance": 1}
        self.clock = clock or (lambda: datetime.now(UTC))
        self.work_providers = []

    async def run(self, request: ScopedRequest, operation):
        start = monotonic()
        control = request.meta.execution
        user = (request.scope.workspace_id, request.scope.user_id)
        lane = control.lane
        user_lane = (*user, lane)
        admitted = False
        parent = _PARENT.get()
        nested = parent is not None and parent[:3] == (id(self), user, lane)
        context_token = None
        reason = "none"
        try:
            if not nested and (
                self.running >= self.max_running
                or self.lanes[lane] >= self.lane_limits[lane]
                or self.user_lanes[user_lane] >= self.user_lane_limits[lane]
            ):
                reason = "overload"
                raise MemoryFault("overloaded", "Memory capacity is busy", retryable=True)
            if not nested:
                self.running += 1
                self.users[user] += 1
                self.lanes[lane] += 1
                self.user_lanes[user_lane] += 1
                admitted = True
            # Audit deadline is checked as well as the monotonic budget. A stale
            # serialized request cannot start another full budget on replay.
            remaining = min(
                control.budget_ms / 1000, (request.meta.deadline_at - self.clock()).total_seconds()
            )
            if parent is not None and parent[0] == id(self) and parent[1] == user:
                remaining = min(remaining, parent[3] - monotonic())
            if remaining <= 0:
                raise TimeoutError
            context_token = _PARENT.set((id(self), user, lane, monotonic() + remaining))
            async with asyncio.timeout(remaining):
                result = await operation()
        except TimeoutError:
            reason = "deadline"
            result = ModuleResult(
                status="error",
                error=ErrorInfo(
                    code="deadline_exceeded", message="Memory deadline exceeded", retryable=True
                ),
            )
        except MemoryFault as exc:
            if exc.info.code == "privacy_changed":
                reason = "privacy_changed"
            result = ModuleResult(status="error", error=exc.info)
        except SQLAlchemyError:
            result = ModuleResult(
                status="error",
                error=ErrorInfo(
                    code="storage_unavailable",
                    message="Memory storage is unavailable",
                    retryable=True,
                ),
            )
        except asyncio.CancelledError:
            # Cancellation propagates; AsyncSession transaction contexts roll back
            # before admission is returned to the next caller.
            raise
        finally:
            if context_token is not None:
                _PARENT.reset(context_token)
            if admitted:
                self.running -= 1
                self.users[user] -= 1
                self.lanes[lane] -= 1
                self.user_lanes[user_lane] -= 1
                if not self.users[user]:
                    del self.users[user]
                if not self.user_lanes[user_lane]:
                    del self.user_lanes[user_lane]
        elapsed_ms = (monotonic() - start) * 1000
        outcome = (
            "timed_out"
            if reason == "deadline"
            else {"ok": "completed", "partial": "partial", "error": "failed"}[result.status]
        )
        handles = [
            handle
            for provider in self.work_providers
            for handle in provider(request.meta.operation_id)
        ]
        result.execution = ExecutionReport(
            operation_id=request.meta.operation_id,
            lane=control.lane,
            budget_profile_id=control.budget_profile_id,
            elapsed_ms=elapsed_ms,
            queue_wait_ms=0.0,
            timings=[
                StageTiming(
                    stage=type(request).__name__,
                    elapsed_ms=elapsed_ms,
                    queue_wait_ms=0.0,
                    outcome=outcome,
                )
            ],
            cancel_reason=reason,
            cleanup="detached_bounded" if handles else "released",
            remaining_work_handles=handles[:16],
            degraded_routes=[],
        )
        return result
