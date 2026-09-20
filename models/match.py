from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class MatchType(str, Enum):
    ONE_TO_ONE = "ONE_TO_ONE"
    MANY_TO_ONE = "MANY_TO_ONE"
    ONE_TO_MANY = "ONE_TO_MANY"


class MatchStatus(str, Enum):
    AUTO_APPROVED = "AUTO_APPROVED"
    PENDING_REVIEW = "PENDING_REVIEW"
    USER_APPROVED = "USER_APPROVED"
    USER_REJECTED = "USER_REJECTED"


class MatchSource(str, Enum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class ReconciliationMatch(BaseModel):
    id: int | None = None
    transaction_id: int
    document_id: int
    confidence_score: float = Field(ge=0.0, le=1.0)
    amount_score: float = Field(ge=0.0, le=1.0)
    date_score: float = Field(ge=0.0, le=1.0)
    vendor_score: float = Field(ge=0.0, le=1.0)
    match_type: MatchType = MatchType.ONE_TO_ONE
    status: MatchStatus = MatchStatus.PENDING_REVIEW
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    match_source: MatchSource = MatchSource.AUTO
    explanation: str | None = None             # Natural language match explanation
    user_action: str | None = None             # approved | rejected | unmatched
    actioned_at: datetime | None = None        # When user took action
    group_id: str | None = None                # UUID string — proof bundle grouping
    covered_amount: str | None = None          # TEXT decimal, None = full coverage
    created_at: datetime | None = None
    # How this pair was decided: "agents" (the 3-agent LLM pipeline) or
    # "deterministic" (an unambiguous pair the agents could not have changed —
    # see core.reconciliation._unambiguous_candidate). A verdict reused from the
    # adjudication memory is still "agents": it is what the agents said, and the
    # audit log's per-call `from_memory` flag is where that distinction lives.
    # Run-local — it travels with the match into the explanation text and the
    # audit log, and is not a database column.
    adjudication: str = "agents"


class ProofItem(BaseModel):
    proof_id: int
    proof_type: Literal["document", "transaction_link"]
    label: str
    confidence_score: float | None = None
    amount: str | None = None
    currency: str | None = None
    date: str | None = None
    vendor: str | None = None
    status: str
    match_source: str
    stored_path: str | None = None
    created_at: str | None = None


class ProofCoverageStats(BaseModel):
    transaction_amount: str
    transaction_currency: str
    total_proof_amount: str
    coverage_ratio: float
    is_fully_covered: bool
    proof_count: int
    document_count: int
    link_count: int


class ProofsResponse(BaseModel):
    transaction_id: int
    proofs: list[ProofItem]
    coverage: ProofCoverageStats
