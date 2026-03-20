from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict


def utc_now() -> datetime:
    return datetime.now(UTC)


class ContractModel(BaseModel):
    """Strict base for every value crossing the Manager/Worker boundary."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
