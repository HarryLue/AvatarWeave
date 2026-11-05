"""A trusted in-process lease context; never accepted from a model or browser."""

from contextvars import ContextVar

from .execution import MemoryFault
from .tables import Job
from .utils import utc

JOB_LEASE = ContextVar("memory_job_lease", default=None)


async def verify_job_lease(db, user_id, now):
    lease = JOB_LEASE.get()
    if lease is None:
        return
    job_id, token, attempt = lease
    row = await db.get(Job, job_id, populate_existing=True)
    if (
        row is None
        or row.account_id != user_id
        or row.state != "running"
        or row.lease_token != token
        or row.attempt != attempt
        or row.lease_until is None
        or utc(row.lease_until) <= now
    ):
        raise MemoryFault("version_conflict", "Background job lease is no longer valid")
