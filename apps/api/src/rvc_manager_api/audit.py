from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditEvent


def add_audit_event(
    session: AsyncSession,
    *,
    actor_type: str,
    action: str,
    resource_type: str,
    actor_id: str | None = None,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details_json=details or {},
    )
    session.add(event)
    return event
