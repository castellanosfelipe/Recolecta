"""Pydantic schemas for local command delegation."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import (
    AuthType,
    ConflictMode,
    PostAction,
    Protocol,
    VerifyMode,
    WindowMode,
)


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


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    client: str = ""
    protocol: Protocol = Protocol.SFTP
    host: str
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str = ""
    secret: str | None = None
    auth_type: AuthType = AuthType.PASSWORD
    key_path: str | None = None
    ssl_mode: str = "preferred"
    remote_paths: tuple[str, ...] = ()
    recursive: bool = False
    max_depth: int = Field(default=3, ge=0)
    include_globs: tuple[str, ...] = ()
    exclude_globs: tuple[str, ...] = ()
    min_size_bytes: int | None = Field(default=None, ge=0)
    max_size_bytes: int | None = Field(default=None, ge=0)
    window_mode: WindowMode = WindowMode.CALENDAR_DAY
    window_hours: int = Field(default=24, ge=1)
    window_overlap_min: int = Field(default=15, ge=0)
    quiet_period_s: int = Field(default=120, ge=0)
    timezone: str = "America/Bogota"
    dest_root: str = "downloads"
    dest_template: str = (
        r"{client}\{connection}\{yyyy}\{MM}\{dd}\{filename}"
    )
    on_conflict: ConflictMode = ConflictMode.SKIP
    verify_mode: VerifyMode = VerifyMode.SIZE
    max_parallel_files: int = Field(default=2, ge=1)
    bandwidth_limit_kbps: int | None = Field(default=None, ge=1)
    timeout_s: float = Field(default=30.0, gt=0)
    retries: int = Field(default=3, ge=0)
    post_action: PostAction = PostAction.NONE
    post_action_path: str | None = None
    enabled: bool = True
    notes: str = ""


class ConnectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    client: str | None = None
    protocol: Protocol | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    secret: str | None = None
    auth_type: AuthType | None = None
    key_path: str | None = None
    ssl_mode: str | None = None
    remote_paths: tuple[str, ...] | None = None
    recursive: bool | None = None
    max_depth: int | None = Field(default=None, ge=0)
    include_globs: tuple[str, ...] | None = None
    exclude_globs: tuple[str, ...] | None = None
    min_size_bytes: int | None = Field(default=None, ge=0)
    max_size_bytes: int | None = Field(default=None, ge=0)
    window_mode: WindowMode | None = None
    window_hours: int | None = Field(default=None, ge=1)
    window_overlap_min: int | None = Field(default=None, ge=0)
    quiet_period_s: int | None = Field(default=None, ge=0)
    timezone: str | None = None
    dest_root: str | None = None
    dest_template: str | None = None
    on_conflict: ConflictMode | None = None
    verify_mode: VerifyMode | None = None
    max_parallel_files: int | None = Field(default=None, ge=1)
    bandwidth_limit_kbps: int | None = Field(default=None, ge=1)
    timeout_s: float | None = Field(default=None, gt=0)
    retries: int | None = Field(default=None, ge=0)
    post_action: PostAction | None = None
    post_action_path: str | None = None
    enabled: bool | None = None
    notes: str | None = None


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any]
