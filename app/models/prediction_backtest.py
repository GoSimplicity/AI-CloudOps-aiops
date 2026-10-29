"""Bounded request contract for evaluating recorded prediction outcomes."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BacktestObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    issued_at: datetime
    target_at: datetime
    observed_at: datetime
    forecast: float = Field(strict=True)
    actual: float = Field(strict=True)


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[BacktestObservation] = Field(min_length=1, max_length=5000)
    window: int = Field(default=200, ge=20, le=1000, strict=True)
    min_history: int = Field(default=20, ge=20, le=1000, strict=True)
