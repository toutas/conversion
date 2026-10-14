from dataclasses import asdict, dataclass
from enum import StrEnum
import json
from typing import TypeVar


class ResponseError(ValueError):
    pass


class Record:
    def to_dict(self) -> dict:
        return asdict(self)  # pyright: ignore[reportArgumentType]


EnumType = TypeVar("EnumType", bound=StrEnum)


@dataclass(frozen=True)
class JsonFields:
    values: dict

    @classmethod
    def from_json(cls, text: str) -> "JsonFields":
        try:
            value = json.loads(text, object_pairs_hook=cls._unique_fields)
        except json.JSONDecodeError as exc:
            raise ResponseError(f"Expected a raw JSON object: {exc}") from exc
        return cls.from_value(value)

    @staticmethod
    def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
        fields = {}
        for key, value in pairs:
            if key in fields:
                raise ResponseError(f"Duplicate JSON field: {key}.")
            fields[key] = value
        return fields

    @classmethod
    def from_value(cls, value: object) -> "JsonFields":
        if not isinstance(value, dict):
            raise ResponseError("Response must be a JSON object.")
        return cls(value)

    def text(self, key: str) -> str:
        value = self.values.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ResponseError(f"{key} must be a nonempty string.")
        return value

    def strings(self, key: str) -> list[str]:
        value = self.values.get(key)
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise ResponseError(f"{key} must be an array of strings.")
        return value

    def objects(self, key: str) -> list["JsonFields"]:
        value = self.values.get(key)
        if not isinstance(value, list):
            raise ResponseError(f"{key} must be an array of objects.")
        return [self.from_value(item) for item in value]

    def choice(self, key: str, enum: type[EnumType]) -> EnumType:
        try:
            return enum(self.text(key))
        except ValueError as exc:
            raise ResponseError(
                f"Invalid {key}; expected one of {[item.value for item in enum]}."
            ) from exc


@dataclass(frozen=True)
class BehaviorMapping(Record):
    source: str
    target: str


@dataclass(frozen=True)
class Dispute(Record):
    finding_id: str
    source_evidence: str


@dataclass(frozen=True)
class Conversion(Record):
    workflow_yaml: str
    assumptions: list[str]
    behavior_mapping: list[BehaviorMapping]
    disputes: list[Dispute]

    def __post_init__(self):
        if not self.workflow_yaml.strip() or not self.behavior_mapping:
            raise ResponseError(
                "A conversion needs nonempty YAML and behavior mappings."
            )

    @classmethod
    def from_json(cls, text: str) -> "Conversion":
        data = JsonFields.from_json(text)
        return cls(
            data.text("workflow_yaml"),
            data.strings("assumptions"),
            [
                BehaviorMapping(item.text("source"), item.text("target"))
                for item in data.objects("behavior_mapping")
            ],
            [
                Dispute(item.text("finding_id"), item.text("source_evidence"))
                for item in data.objects("disputes")
            ],
        )


class Severity(StrEnum):
    BLOCKER = "blocker"
    ADVISORY = "advisory"


class Verdict(StrEnum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Finding(Record):
    id: str
    severity: Severity
    message: str
    source_evidence: str
    requested_change: str


@dataclass(frozen=True)
class Review(Record):
    verdict: Verdict
    findings: list[Finding]
    limitations: list[str]

    def __post_init__(self):
        ids = [finding.id for finding in self.findings]
        if len(ids) != len(set(ids)):
            raise ResponseError("Finding IDs must be unique.")
        blockers = any(
            finding.severity == Severity.BLOCKER for finding in self.findings
        )
        if self.verdict == Verdict.APPROVE and blockers:
            raise ResponseError("Approval cannot contain blockers.")
        if self.verdict != Verdict.APPROVE and not blockers:
            raise ResponseError("Rejection needs a blocker.")

    @classmethod
    def from_json(cls, text: str) -> "Review":
        data = JsonFields.from_json(text)
        findings = [
            Finding(
                item.text("id"),
                item.choice("severity", Severity),
                item.text("message"),
                item.text("source_evidence"),
                item.text("requested_change"),
            )
            for item in data.objects("findings")
        ]
        return cls(
            data.choice("verdict", Verdict), findings, data.strings("limitations")
        )
