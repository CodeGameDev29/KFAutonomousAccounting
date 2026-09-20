from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class VendorRule(BaseModel):
    id: int | None = None
    vendor_pattern: str
    category: str
    priority: int = Field(default=50)
    source: str = Field(default="manual", pattern=r"^(manual|learned)$")
    created_at: datetime | None = None
