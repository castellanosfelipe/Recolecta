"""Pydantic schemas for local command delegation."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class RunNowRequest(BaseModel):
    connection_id: int | None = Field(default=None, ge=1)
    selected_date: date | None = None
    dry_run: bool = False
    trigger: Literal["manual", "cli"] = "manual"


class RunNowResponse(BaseModel):
    executions: list[dict[str, object]]


class CancelResponse(BaseModel):
    run_id: int
    cancelled: bool
