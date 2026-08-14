from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class UsagePriceSnapshotCreate(BaseModel):
    subject_type: Literal["model", "tool", "sandbox", "storage", "egress"]
    provider: str | None = Field(default=None, min_length=1, max_length=191)
    subject_key: str = Field(min_length=1, max_length=191)
    metric: str = Field(min_length=1, max_length=64)
    unit: str = Field(min_length=1, max_length=64)
    currency: str = Field(min_length=3, max_length=3)
    price_per_unit: Decimal = Field(ge=0, max_digits=30, decimal_places=12)
    price_status: Literal["estimated", "contracted"]
    effective_from: datetime
    effective_to: datetime | None = None
    source_label: str = Field(min_length=1, max_length=500)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.upper()
        if not normalized.isalpha():
            raise ValueError("currency must be a three-letter code")
        return normalized

    @model_validator(mode="after")
    def validate_window_and_subject(self):
        if self.subject_type == "model" and not self.provider:
            raise ValueError("model prices require provider")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self


class UsageAdjustmentCreate(BaseModel):
    original_entry_id: str = Field(min_length=1, max_length=36)
    quantity_delta: Decimal = Field(max_digits=30, decimal_places=9)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("quantity_delta")
    @classmethod
    def nonzero_delta(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("quantity_delta must be non-zero")
        return value
