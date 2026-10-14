import hashlib
import json
from pathlib import Path
import shutil

import pytest

from conversion.cli import CommandLine
from conversion.models import Check
from conversion.responses import Conversion
from conversion.validation import Workflow, WorkflowValidator


EVALUATIONS = Path(__file__).parent / "evaluations"
FIXTURES = Path(__file__).parent / "fixtures"


class RecordedAgent:
    def __init__(self, recording):
        self.backend = json.loads((recording / "backend.json").read_text())
        self.responses = iter(
            (role, path)
            for directory in sorted(recording.glob("round-*"))
            for role in ("converter", "reviewer")
            if (path := directory / f"{role}.txt").exists()
        )
        self.calls = []

    @property
    def metadata(self):
        return {
            **self.backend,
            "completed_calls": self.backend["completed_calls"][:len(self.calls)],
        }

    def complete(self, role, prompt):
        expected_role, path = next(self.responses)
        assert role == expected_role
        data = json.loads(prompt.split("INPUT DATA (JSON):\n")[1])
        self.calls.append((role, data))
        return path.read_text(encoding="utf-8")


class RecordedChecks:
    def __init__(self, recording):
        self.recording = recording
        self.calls = []

    def check(self, path):
        original = self.recording / path.parent.name
        assert path.read_bytes() == (original / "workflow.yml").read_bytes()
        self.calls.append(path)
        return [Check(**item) for item in json.loads((original / "checks.json").read_text())]


@pytest.mark.parametrize("case, expected_verdicts, expected_calls", [
    ("basic", ["request_changes", "approve"], 4),
    ("container-parallel", [None, "request_changes", "approve"], 5),
])
def test_luna_high_recordings_replay_through_class_interfaces(
    workspace, monkeypatch, case, expected_verdicts, expected_calls,
):
    recording = EVALUATIONS / case / "luna-high"
    recorded_report = json.loads((recording / "report.json").read_text())
    agent = RecordedAgent(recording)
    validator = RecordedChecks(recording)

    def create_client(timeout):
        assert timeout == agent.backend["timeout_per_call_seconds"]
        return agent

    def create_validator(require_actionlint):
        assert require_actionlint
        return validator

    monkeypatch.setattr("conversion.cli.CopilotClient.create", create_client)
    monkeypatch.setattr("conversion.cli.WorkflowValidator.create", create_validator)
    source = FIXTURES / f"{case}.Jenkinsfile"
    original = source.read_bytes()
    output = workspace / case
    assert CommandLine.run([
        str(source), "--output", str(output),
        "--max-rounds", "3", "--timeout", "360", "--require-actionlint",
    ]) == 0
    replayed = json.loads((output / "report.json").read_text())
    assert replayed["status"] == recorded_report["status"] == "accepted_source_only"
    assert replayed["source_sha256"] == recorded_report["source_sha256"]
    assert replayed["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert (output / "input.Jenkinsfile").read_bytes() == original == source.read_bytes()
    assert (output / "workflow.yml").read_bytes() == (recording / "workflow.yml").read_bytes()
    assert replayed["assurance"] == recorded_report["assurance"]
    assert replayed["error"] is None
    assert len(replayed["rounds"]) == len(expected_verdicts) == len(validator.calls)
    assert len(agent.calls) == expected_calls
    assert [role for role, _ in agent.calls] == [
        call["role"] for call in agent.backend["completed_calls"]
    ]
    assert [
        None if item["review"] is None else item["review"]["verdict"]
        for item in replayed["rounds"]
    ] == expected_verdicts
    assert Path(replayed["accepted_workflow"]) == output / "workflow.yml"
    assert Path(replayed["last_candidate"]).parent.name == f"round-{len(expected_verdicts):02}"
    for actual, expected in zip(replayed["rounds"], recorded_report["rounds"], strict=True):
        assert {key: actual[key] for key in expected} == expected
        directory = f"round-{actual['number']:02}"
        for original_file in (recording / directory).iterdir():
            replay_file = output / directory / original_file.name
            if original_file.suffix == ".json":
                assert json.loads(replay_file.read_text()) == json.loads(original_file.read_text())
            else:
                assert replay_file.read_bytes() == original_file.read_bytes()
    backend = json.loads((output / "backend.json").read_text())
    assert backend == agent.backend
    assert backend["converter_model_requested"] == backend["reviewer_model_requested"] == "gpt-6-luna"
    assert backend["reasoning_effort_requested"] == "high"
    assert all(call["model"] == "gpt-6-luna" for call in backend["completed_calls"])
    assert next(agent.responses, None) is None

    converter_inputs = [data for role, data in agent.calls if role == "converter"]
    reviewer_inputs = [data for role, data in agent.calls if role == "reviewer"]
    assert converter_inputs[0]["previous_attempt"] is None
    assert all("behavior_mapping" not in data for data in reviewer_inputs)
    final = (output / "workflow.yml").read_text()
    if case == "basic":
        finding = converter_inputs[1]["previous_attempt"]["review"]["findings"][0]
        assert finding["id"] == "reporter-missing-checks-permission"
        assert "checks: write" in final and "contents: read" in final
    else:
        failed_checks = converter_inputs[1]["previous_attempt"]["checks"]
        assert failed_checks[-1]["status"] == "fail"
        assert 'input "if-no-artifact-found" is not defined' in failed_checks[-1]["detail"]
        assert converter_inputs[1]["previous_attempt"]["review"] is None
        assert "alpine-javascript-action-runtime" in json.dumps(converter_inputs[2])
        assert "docker run" in final
        assert all(
            "container" not in job for job in Workflow.from_yaml(final).document["jobs"].values()
        )


def test_relocated_structured_paths_resolve_and_sources_match_snapshots():
    recordings = [
        EVALUATIONS / case / profile
        for case in ("basic", "container-parallel")
        for profile in ("initial-text", "json", "luna-high")
    ]
    for recording in recordings:
        snapshot = recording / "input.Jenkinsfile"
        assert snapshot.read_bytes() == (FIXTURES / f"{recording.parent.name}.Jenkinsfile").read_bytes()
        path = recording / "report.json"
        if recording == EVALUATIONS / "container-parallel/json":
            assert not path.exists()
            continue
        report = json.loads(path.read_text())
        assert report["source_sha256"] == hashlib.sha256(
            (path.parent / "input.Jenkinsfile").read_bytes(),
        ).hexdigest()
        for field in ("accepted_workflow", "last_candidate"):
            if report[field] is not None:
                relocated = Path(report[field])
                assert not relocated.is_absolute() and ".." not in relocated.parts
                assert relocated.is_file(), (path, field, relocated)
                assert relocated.resolve().is_relative_to(path.parent.resolve())


@pytest.mark.parametrize("case", ["basic", "container-parallel"])
def test_recorded_candidates_with_real_actionlint(case):
    if shutil.which("actionlint") is None:
        pytest.skip("actionlint is optional; recorded replay still runs without it.")
    recording = EVALUATIONS / case / "luna-high"
    validator = WorkflowValidator.create(require_actionlint=True)
    for directory in sorted(recording.glob("round-*")):
        original = json.loads((directory / "checks.json").read_text())
        checks = validator.check(directory / "workflow.yml")
        assert [(check.name, check.status) for check in checks] == [
            (check["name"], check["status"]) for check in original
        ]
        conversion = Conversion.from_json((directory / "converter.txt").read_text())
        assert conversion.to_dict() == json.loads((directory / "conversion.json").read_text())
        assert conversion.workflow_yaml.encode() == (directory / "workflow.yml").read_bytes()
        if case == "container-parallel" and directory.name == "round-01":
            assert 'input "if-no-artifact-found" is not defined' in checks[-1].detail
