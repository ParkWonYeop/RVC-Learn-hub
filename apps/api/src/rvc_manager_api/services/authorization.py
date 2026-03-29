from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Experiment, Job, User


async def require_job_owner_or_admin(
    session: AsyncSession,
    *,
    job_id: str,
    user: User,
) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    experiment = await session.get(Experiment, job.experiment_id)
    if experiment is None or (user.role != "admin" and experiment.created_by != user.id):
        raise HTTPException(status_code=404, detail="job not found")
    return job
