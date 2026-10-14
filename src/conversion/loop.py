import hashlib
from pathlib import Path
from typing import Protocol

from copilot_client import BackendError, Role

from .artifacts import Artifacts
from .models import Report, Round, Status
from .prompts import Prompts
from .responses import Conversion, ResponseError, Review, Verdict
from .validation import Validator, WorkflowValidator


class Agent(Protocol):
    def complete(self, role: Role, prompt: str) -> str: ...


class ConversionLoop:
    def __init__(self, artifacts: Artifacts, source: str, agent: Agent,
                 validator: Validator, max_rounds: int):
        self.artifacts = artifacts
        self.source = source
        self.agent = agent
        self.validator = validator
        self.max_rounds = max_rounds
        self.report = Report.from_source(source)
        self._feedback: dict | None = None
        self._previous_review: Review | None = None
        self._seen: set[str] = set()
        self._started = False

    @classmethod
    def create(cls, source: str, output: Path, agent: Agent, max_rounds: int = 3,
               validator: Validator | None = None) -> "ConversionLoop":
        if not source.strip():
            raise ValueError("Jenkinsfile must not be empty.")
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds < 1:
            raise ValueError("max_rounds must be a positive integer.")
        if validator is None:
            validator = WorkflowValidator.create()
        return cls(Artifacts.create(output, source), source, agent, validator, max_rounds)

    def run(self) -> Report:
        if self._started:
            raise ValueError("A conversion loop can only run once.")
        self._started = True
        try:
            for number in range(1, self.max_rounds + 1):
                round_report = Round(number)
                self.report.rounds.append(round_report)
                directory = self.artifacts.create_round(number)
                conversion = self._convert(directory, round_report)
                if conversion is None:
                    continue
                candidate = self.artifacts.write_text(
                    directory / "workflow.yml", conversion.workflow_yaml,
                )
                self.report.last_candidate = str(candidate)
                round_report.checks = self.validator.check(candidate)
                checks = [check.to_dict() for check in round_report.checks]
                self.artifacts.write_json(directory / "checks.json", checks)
                self._feedback = {
                    "candidate": conversion.to_dict(), "checks": checks,
                    "review": None if self._previous_review is None else self._previous_review.to_dict(),
                }
                if any(check.status == "fail" for check in round_report.checks):
                    continue
                review = self._review(directory, round_report, conversion)
                if review is None or self._finish(review, conversion):
                    break
        except BackendError as exc:
            self.report.status = Status.BACKEND_ERROR
            self.report.error = str(exc)
        self.artifacts.write_json("report.json", self.report.to_dict())
        return self.report

    def _convert(self, directory: Path, round_report: Round) -> Conversion | None:
        raw = self.agent.complete("converter", Prompts.build(Prompts.CONVERTER, {
            "jenkinsfile": self.source, "previous_attempt": self._feedback,
        }))
        self.artifacts.write_text(directory / "converter.txt", raw)
        try:
            conversion = Conversion.from_json(raw)
        except ResponseError as exc:
            round_report.error = str(exc)
            self._feedback = {
                "response_error": str(exc), "previous_response": raw,
                "previous_attempt": self._feedback,
            }
            return None
        self.artifacts.write_json(directory / "conversion.json", conversion.to_dict())
        return conversion

    def _review(self, directory: Path, round_report: Round, conversion: Conversion) -> Review | None:
        if self._feedback is None:
            raise RuntimeError("Review requires a validated conversion.")
        raw = self.agent.complete("reviewer", Prompts.build(Prompts.REVIEWER, {
            "jenkinsfile": self.source,
            "workflow_yaml": conversion.workflow_yaml,
            "declared_assumptions": conversion.assumptions,
            "checks": [check.to_dict() for check in round_report.checks],
            "previous_review": None if self._previous_review is None else self._previous_review.to_dict(),
            "disputes": [dispute.to_dict() for dispute in conversion.disputes],
        }))
        self.artifacts.write_text(directory / "reviewer.txt", raw)
        try:
            review = Review.from_json(raw)
        except ResponseError as exc:
            round_report.error = str(exc)
            self.report.error = str(exc)
            self.report.status = Status.INVALID_REVIEW
            return None
        round_report.review = review
        self._previous_review = review
        self.artifacts.write_json(directory / "review.json", review.to_dict())
        self._feedback["review"] = review.to_dict()
        return review

    def _finish(self, review: Review, conversion: Conversion) -> bool:
        if review.verdict == Verdict.APPROVE:
            path = self.artifacts.write_text("workflow.yml", conversion.workflow_yaml)
            self.report.status = Status.ACCEPTED
            self.report.accepted_workflow = str(path)
            return True
        if review.verdict == Verdict.BLOCKED:
            self.report.status = Status.BLOCKED
            return True
        fingerprint = hashlib.sha256(conversion.workflow_yaml.encode()).hexdigest()
        if fingerprint in self._seen:
            self.report.status = Status.STALLED
            return True
        self._seen.add(fingerprint)
        return False
