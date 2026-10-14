from dataclasses import dataclass, field
from enum import StrEnum
import hashlib

from .responses import Record, Review


ASSURANCE = (
    "Source-only evaluation, not verified Jenkins/GitHub Actions runtime equivalence. "
    "The reviewer and behavior mapping are LLM judgments, not deterministic proof."
)


class Status(StrEnum):
    ACCEPTED = "accepted_source_only"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"
    STALLED = "stalled"
    INVALID_REVIEW = "invalid_review"
    BACKEND_ERROR = "backend_error"


@dataclass(frozen=True)
class Check(Record):
    name: str
    status: str
    detail: str


@dataclass
class Round(Record):
    number: int
    checks: list[Check] = field(default_factory=list)
    review: Review | None = None
    error: str | None = None


@dataclass
class Report(Record):
    source_sha256: str
    status: Status = Status.EXHAUSTED
    assurance: str = ASSURANCE
    rounds: list[Round] = field(default_factory=list)
    accepted_workflow: str | None = None
    last_candidate: str | None = None
    error: str | None = None

    @classmethod
    def from_source(cls, source: str) -> "Report":
        if not source.strip():
            raise ValueError("Jenkinsfile must not be empty.")
        return cls(hashlib.sha256(source.encode()).hexdigest())
