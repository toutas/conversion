import json
from pathlib import Path
import subprocess
import sys

import pytest

from copilot_client import BackendError
from conversion.cli import CommandLine
from conversion.validation import WorkflowValidator


class OfflineClient:
    def __init__(self, conversion_data, response):
        self.conversion_data = conversion_data
        self.response = response
        self.calls = []

    @property
    def metadata(self):
        return {"backend": "offline_test", "completed_calls": self.calls}

    def complete(self, role, prompt):
        if isinstance(self.response, BackendError):
            raise self.response
        self.calls.append({"role": role})
        return json.dumps(self.conversion_data) if role == "converter" else self.response


@pytest.fixture
def offline_setup(workspace, conversion_data, monkeypatch):
    client = OfflineClient(
        conversion_data, '{"verdict": "approve", "findings": [], "limitations": []}',
    )
    source = workspace / "source.Jenkinsfile"
    source.write_bytes(b"pipeline {\r\n    /* immutable */\r\n}\r\n")
    monkeypatch.setattr("conversion.cli.CopilotClient.create", lambda timeout: client)
    monkeypatch.setattr(
        "conversion.cli.WorkflowValidator.create",
        lambda require_actionlint: WorkflowValidator(actionlint=None),
    )
    return client, source


def test_cli_success_writes_report_and_backend_usage(workspace, offline_setup, capsys):
    client, source = offline_setup
    output = workspace / "run"
    original = source.read_bytes()
    assert CommandLine.run([str(source), "--output", str(output)]) == 0
    assert "accepted_source_only" in capsys.readouterr().out
    assert (output / "input.Jenkinsfile").read_bytes() == original == source.read_bytes()
    assert json.loads((output / "backend.json").read_text()) == {
        **client.metadata, "max_rounds": 3,
    }


@pytest.mark.parametrize("response, status", [
    ('{"verdict": "approve"}', "invalid_review"),
    (BackendError("Authentication failed"), "backend_error"),
    ('{"verdict": "request_changes", "findings": [{"id": "x", "severity": "blocker",'
     '"message": "Missing test", "source_evidence": "stage Test",'
     '"requested_change": "Add test"}], "limitations": []}', "exhausted"),
])
def test_cli_nonaccepted_outcomes_return_one(workspace, offline_setup, response, status):
    client, source = offline_setup
    client.response = response
    output = workspace / "run"
    assert CommandLine.run([str(source), "--output", str(output), "--max-rounds", "1"]) == 1
    assert json.loads((output / "report.json").read_text())["status"] == status
    assert (output / "backend.json").is_file()
    assert not (output / "workflow.yml").exists()


@pytest.mark.parametrize("flag, value", [
    ("--converter-model", "gpt-6-sol"),
    ("--reviewer-model", "gpt-6-sol"),
    ("--reasoning-effort", "low"),
    ("--max-rounds", "0"), ("--max-rounds", "-1"), ("--max-rounds", "1.5"),
    ("--max-rounds", "true"), ("--timeout", "0"), ("--timeout", "-1"),
    ("--timeout", "nan"), ("--timeout", "inf"),
])
def test_disabled_flags_and_invalid_limits_fail_before_setup(workspace, flag, value, monkeypatch):
    def unexpected_client(timeout):
        pytest.fail("Invalid arguments must fail before client setup.")

    monkeypatch.setattr("conversion.cli.CopilotClient.create", unexpected_client)
    with pytest.raises(SystemExit) as exc:
        CommandLine.run(["input", "--output", str(workspace / "run"), flag, value])
    assert exc.value.code == 2
    assert not (workspace / "run").exists()


@pytest.mark.parametrize("source_bytes", [b"", b" \n\t", b"\xff"])
def test_bad_source_returns_two_without_calls(workspace, offline_setup, source_bytes):
    client, source = offline_setup
    source.write_bytes(source_bytes)
    assert CommandLine.run([str(source), "--output", str(workspace / "run")]) == 2
    assert not client.calls
    assert not (workspace / "run").exists()


def test_missing_source_returns_two(workspace):
    assert CommandLine.run(["missing-source", "--output", str(workspace / "run")]) == 2
    assert not (workspace / "run").exists()


def test_existing_output_returns_two_without_calls(workspace, offline_setup):
    client, source = offline_setup
    assert CommandLine.run([str(source), "--output", str(workspace)]) == 2
    assert not client.calls
    assert not (workspace / "input.Jenkinsfile").exists()


def test_missing_required_checker_fails_before_client_creation(workspace, monkeypatch):
    source = workspace / "source.Jenkinsfile"
    source.write_text("pipeline {}")
    monkeypatch.setattr("conversion.validation.shutil.which", lambda _: None)

    def unexpected_client(timeout):
        pytest.fail("Missing required actionlint must fail before client setup.")

    monkeypatch.setattr("conversion.cli.CopilotClient.create", unexpected_client)
    assert CommandLine.run([
        str(source), "--output", str(workspace / "run"), "--require-actionlint",
    ]) == 2
    assert not (workspace / "run").exists()


def test_missing_copilot_fails_without_creating_output(workspace, monkeypatch):
    source = workspace / "source.Jenkinsfile"
    source.write_text("pipeline {}")
    monkeypatch.setattr("copilot_client.client.shutil.which", lambda _: None)
    assert CommandLine.run([str(source), "--output", str(workspace / "run")]) == 2
    assert not (workspace / "run").exists()


def test_module_help_and_parser_errors_are_offline(workspace, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path("src").resolve()))
    for arguments, exitcode in [
        (["--help"], 0),
        (["missing", "--output", str(workspace / "run"), "--timeout", "nan"], 2),
        (["missing", "--output", str(workspace / "run"), "--converter-model", "other"], 2),
    ]:
        result = subprocess.run(
            [sys.executable, "-m", "conversion", *arguments],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == exitcode, result.stderr
        if exitcode == 0:
            assert "--max-rounds" in result.stdout and "--require-actionlint" in result.stdout
            assert "--converter-model" not in result.stdout
            assert "--reasoning-effort" not in result.stdout
    assert not (workspace / "run").exists()
