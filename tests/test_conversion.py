import hashlib
import json
from pathlib import Path, PureWindowsPath

import pytest

from copilot_client import BackendError
from conversion import ConversionLoop
from conversion.artifacts import Artifacts
from conversion.models import Status
from conversion.validation import WorkflowValidator


class ScriptedAgent:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def complete(self, role, prompt):
        self.calls.append((role, prompt))
        expected_role, response = next(self.responses)
        assert role == expected_role
        if isinstance(response, BackendError):
            raise response
        return response


@pytest.fixture
def validator():
    return WorkflowValidator(actionlint=None)


@pytest.fixture
def candidate(conversion_data):
    return json.dumps(conversion_data)


@pytest.fixture
def approval():
    return json.dumps(
        {
            "verdict": "approve",
            "findings": [],
            "limitations": ["No runtime equivalence was verified."],
        }
    )


def test_approval_is_source_only_and_preserves_artifacts(
    workspace,
    validator,
    candidate,
    approval,
    workflow,
):
    source = "pipeline {\r\n    /* build */\r\n}\r\n"
    agent = ScriptedAgent([("converter", candidate), ("reviewer", approval)])
    output = workspace / "run"
    loop = ConversionLoop.create(source, output, agent, validator=validator)
    report = loop.run()
    assert report.status == Status.ACCEPTED
    assert "not verified" in report.assurance
    assert report.source_sha256 == hashlib.sha256(source.encode()).hexdigest()
    assert (output / "workflow.yml").read_text() == workflow
    assert (output / "input.Jenkinsfile").read_bytes() == source.encode()
    assert (output / "round-01/converter.txt").read_text() == candidate
    assert (output / "round-01/reviewer.txt").read_text() == approval
    assert json.loads((output / "report.json").read_text()) == report.to_dict()
    assert report.rounds[0].checks[-1].status == "not_run"
    reviewer_input = json.loads(agent.calls[1][1].split("INPUT DATA (JSON):\n")[1])
    assert reviewer_input["jenkinsfile"] == source
    assert "behavior_mapping" not in reviewer_input
    assert "previous_attempt" not in reviewer_input
    with pytest.raises(ValueError, match="only run once"):
        loop.run()
    assert len(agent.calls) == 2


def test_unchanged_candidate_can_dispute_a_review(
    workspace,
    validator,
    candidate,
    approval,
    conversion_data,
    review_data,
):
    conversion_data["disputes"] = [
        {
            "finding_id": "missing-tests",
            "source_evidence": "The source delegates tests to the build command.",
        }
    ]
    agent = ScriptedAgent(
        [
            ("converter", candidate),
            ("reviewer", json.dumps(review_data)),
            ("converter", json.dumps(conversion_data)),
            ("reviewer", approval),
        ]
    )
    report = ConversionLoop.create(
        "pipeline {}",
        workspace / "run",
        agent,
        validator=validator,
    ).run()
    assert report.status == Status.ACCEPTED
    assert len(report.rounds) == 2
    converter_input = json.loads(agent.calls[2][1].split("INPUT DATA (JSON):\n")[1])
    reviewer_input = json.loads(agent.calls[3][1].split("INPUT DATA (JSON):\n")[1])
    assert converter_input["previous_attempt"]["review"] == review_data
    assert reviewer_input["previous_review"] == review_data
    assert reviewer_input["disputes"] == conversion_data["disputes"]


@pytest.mark.parametrize(
    "verdict, expected",
    [
        ("blocked", Status.BLOCKED),
        ("request_changes", Status.EXHAUSTED),
    ],
)
def test_rejection_never_publishes_top_level_workflow(
    workspace,
    validator,
    candidate,
    review_data,
    verdict,
    expected,
):
    review_data["verdict"] = verdict
    agent = ScriptedAgent(
        [("converter", candidate), ("reviewer", json.dumps(review_data))]
    )
    output = workspace / "run"
    report = ConversionLoop.create(
        "pipeline {}",
        output,
        agent,
        max_rounds=1,
        validator=validator,
    ).run()
    assert report.status == expected
    assert report.accepted_workflow is None
    assert not (output / "workflow.yml").exists()
    assert Path(report.last_candidate).is_file()  # pyright: ignore[reportArgumentType]


def test_repeated_rejected_candidate_stalls(
    workspace, validator, candidate, review_data
):
    rejection = json.dumps(review_data)
    agent = ScriptedAgent(
        [
            ("converter", candidate),
            ("reviewer", rejection),
            ("converter", candidate),
            ("reviewer", rejection),
        ]
    )
    report = ConversionLoop.create(
        "pipeline {}",
        workspace / "run",
        agent,
        validator=validator,
    ).run()
    assert report.status == Status.STALLED
    assert len(report.rounds) == 2
    assert len(agent.calls) == 4


def test_advisories_allow_immediate_acceptance(
    workspace,
    validator,
    candidate,
    review_data,
):
    review_data["verdict"] = "approve"
    review_data["findings"][0]["severity"] = "advisory"
    agent = ScriptedAgent(
        [("converter", candidate), ("reviewer", json.dumps(review_data))]
    )
    report = ConversionLoop.create(
        "pipeline {}",
        workspace / "run",
        agent,
        validator=validator,
    ).run()
    assert report.status == Status.ACCEPTED
    assert len(agent.calls) == 2


def test_bad_converter_json_can_be_repaired(
    workspace,
    validator,
    candidate,
    approval,
):
    agent = ScriptedAgent(
        [
            ("converter", "not JSON"),
            ("converter", candidate),
            ("reviewer", approval),
        ]
    )
    report = ConversionLoop.create(
        "pipeline {}",
        workspace / "run",
        agent,
        validator=validator,
    ).run()
    assert report.status == Status.ACCEPTED
    assert report.rounds[0].error
    assert "Expected a raw JSON object" in agent.calls[1][1]
    assert (workspace / "run/round-01/converter.txt").read_text() == "not JSON"
    assert not (workspace / "run/round-01/workflow.yml").exists()


def test_outstanding_review_survives_malformed_and_static_failure_rounds(
    workspace,
    validator,
    candidate,
    approval,
    conversion_data,
    review_data,
):
    conversion_data["workflow_yaml"] = "jobs: ["
    agent = ScriptedAgent(
        [
            ("converter", candidate),
            ("reviewer", json.dumps(review_data)),
            ("converter", "not JSON"),
            ("converter", json.dumps(conversion_data)),
            ("converter", candidate),
            ("reviewer", approval),
        ]
    )
    report = ConversionLoop.create(
        "pipeline {}",
        workspace / "run",
        agent,
        max_rounds=4,
        validator=validator,
    ).run()
    assert report.status == Status.ACCEPTED
    assert report.rounds[1].error
    assert report.rounds[2].checks[0].status == "fail"
    assert report.rounds[2].review is None
    converter_input = json.loads(agent.calls[4][1].split("INPUT DATA (JSON):\n")[1])
    reviewer_input = json.loads(agent.calls[5][1].split("INPUT DATA (JSON):\n")[1])
    assert converter_input["previous_attempt"]["review"] == review_data
    assert reviewer_input["previous_review"] == review_data


def test_static_failure_skips_reviewer(workspace, validator, conversion_data):
    conversion_data["workflow_yaml"] = "jobs: ["
    agent = ScriptedAgent([("converter", json.dumps(conversion_data))])
    report = ConversionLoop.create(
        "pipeline {}",
        workspace / "run",
        agent,
        max_rounds=1,
        validator=validator,
    ).run()
    assert report.status == Status.EXHAUSTED
    assert report.rounds[0].checks[0].status == "fail"
    assert report.rounds[0].review is None
    assert len(agent.calls) == 1
    assert not (workspace / "run/workflow.yml").exists()


@pytest.mark.parametrize(
    "response",
    [
        "not JSON",
        '{"verdict": "approve"}',
        '{"verdict": "request_changes", "findings": [], "limitations": []}',
    ],
)
def test_invalid_review_never_approves(workspace, validator, candidate, response):
    agent = ScriptedAgent([("converter", candidate), ("reviewer", response)])
    report = ConversionLoop.create(
        "pipeline {}",
        workspace / "run",
        agent,
        validator=validator,
    ).run()
    assert report.status == Status.INVALID_REVIEW
    assert report.error == report.rounds[0].error
    assert report.error
    assert not (workspace / "run/workflow.yml").exists()


@pytest.mark.parametrize("role", ["converter", "reviewer"])
def test_backend_error_is_persisted(workspace, validator, candidate, role):
    responses = [] if role == "converter" else [("converter", candidate)]
    responses.append((role, BackendError("Authentication failed.")))
    report = ConversionLoop.create(
        "pipeline {}",
        workspace / "run",
        ScriptedAgent(responses),
        validator=validator,
    ).run()
    assert report.status == Status.BACKEND_ERROR
    assert report.error == "Authentication failed."
    assert json.loads((workspace / "run/report.json").read_text()) == report.to_dict()
    assert not (workspace / "run/workflow.yml").exists()


@pytest.mark.parametrize(
    "source, rounds",
    [
        ("", 1),
        (" \n\t", 1),
        ("pipeline {}", 0),
        ("pipeline {}", -1),
        ("pipeline {}", True),
        ("pipeline {}", False),
        ("pipeline {}", 1.5),
        ("pipeline {}", 2.0),
        ("pipeline {}", "3"),
    ],
)
def test_invalid_factory_inputs_have_no_side_effects(workspace, source, rounds):
    output = workspace / "run"
    agent = ScriptedAgent([])
    with pytest.raises(ValueError):
        ConversionLoop.create(source, output, agent, max_rounds=rounds)
    assert not output.exists()
    assert not agent.calls


def test_existing_outputs_are_untouched(workspace, validator):
    sentinel = workspace / "existing"
    sentinel.write_bytes(b"existing evidence")
    with pytest.raises(FileExistsError):
        ConversionLoop.create(
            "pipeline {}", workspace, ScriptedAgent([]), validator=validator
        )
    assert sentinel.read_bytes() == b"existing evidence"
    assert not (workspace / "input.Jenkinsfile").exists()


@pytest.mark.parametrize("name", ["../outside", "round-01/../../outside", "/absolute"])
def test_artifact_names_cannot_escape_the_run(workspace, name):
    artifacts = Artifacts.create(workspace / "run", "pipeline {}")
    with pytest.raises(ValueError, match="within the run"):
        artifacts.write_text(name, "bad")


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        r"..\outside",
        r"round-01\..\..\outside",
        "/absolute",
        r"\absolute",
        "C:/absolute",
        r"C:\absolute",
        "C:relative",
        "D:relative",
        r"\\server\share\absolute",
    ],
)
def test_artifact_names_cannot_escape_a_windows_run(monkeypatch, name):
    monkeypatch.setattr("conversion.artifacts.Path", PureWindowsPath)
    artifacts = Artifacts(
        PureWindowsPath("C:/run")  # pyright: ignore[reportArgumentType]
    )
    with pytest.raises(ValueError, match="within the run"):
        artifacts.path(name)


@pytest.mark.parametrize(
    "name",
    [
        "workflow.yml",
        "round-01/converter.txt",
        r"round-01\converter.txt",
    ],
)
def test_relative_windows_artifact_paths_stay_within_the_run(monkeypatch, name):
    monkeypatch.setattr("conversion.artifacts.Path", PureWindowsPath)
    artifacts = Artifacts(
        PureWindowsPath("C:/run")  # pyright: ignore[reportArgumentType]
    )
    assert artifacts.path(name) == PureWindowsPath("C:/run") / name
