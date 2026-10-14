import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from copilot_client import BackendError, CopilotClient, CopilotResponse


def events(content, model=CopilotClient.MODEL):
    return "\n".join(json.dumps(event) for event in [
        {"type": "assistant.message", "data": {
            "content": content, "model": model, "toolRequests": [],
        }},
        {"type": "result", "exitCode": 0, "usage": {"premiumRequests": 1}},
    ])


class FakeProcess:
    returncode = 0
    pid = 123

    def __init__(self, stdout=None, stderr=""):
        self.stdout = events('{"result": "example"}') if stdout is None else stdout
        self.stderr = stderr
        self.timeouts = []
        self.killed = False

    def communicate(self, timeout=None):
        self.timeouts.append(timeout)
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


@pytest.fixture
def process(monkeypatch):
    process = FakeProcess()
    monkeypatch.setattr("copilot_client.client.shutil.which", lambda _: "/fake/copilot")
    monkeypatch.setattr("copilot_client.client.subprocess.Popen", lambda *a, **kw: process)
    return process


def test_fresh_role_isolation_with_locked_model_and_known_working_tool_flags(monkeypatch):
    calls = []
    monkeypatch.setattr("copilot_client.client.shutil.which", lambda _: "/fake/copilot")

    def popen(command, **kwargs):
        directory = Path(kwargs["cwd"])
        assert directory.parent == Path.cwd()
        assert directory.is_dir() and not list(directory.iterdir())
        (directory / "session-marker").write_text("private to this call")
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr("copilot_client.client.subprocess.Popen", popen)
    client = CopilotClient.create(timeout=12)
    assert client.complete("converter", "source") == '{"result": "example"}'
    client.complete("reviewer", "candidate")
    assert calls[0][1]["cwd"] != calls[1][1]["cwd"]
    for command, kwargs in calls:
        assert set([
            "--silent", "--stream=off", "--output-format=json", "--no-color",
            "--no-auto-update", "--no-custom-instructions", "--no-ask-user",
            "--disable-builtin-mcps", "--available-tools=", "--allow-all-tools",
            "--deny-tool=shell", "--deny-tool=write", "--deny-tool=url",
        ]).issubset(command)
        assert command[command.index("--model") + 1] == "gpt-6-luna"
        assert command[command.index("--reasoning-effort") + 1] == "high"
        assert not any(flag.startswith(("--resume", "--continue")) for flag in command)
        assert "shell" not in kwargs
        assert kwargs["start_new_session"] == (os.name == "posix")
        assert not Path(kwargs["cwd"]).exists()
    assert client.metadata == {
        "backend": "copilot_cli",
        "converter_model_requested": "gpt-6-luna",
        "reviewer_model_requested": "gpt-6-luna",
        "reasoning_effort_requested": "high",
        "timeout_per_call_seconds": 12,
        "completed_calls": [
            {"role": role, "model": "gpt-6-luna", "usage": {"premiumRequests": 1}}
            for role in ("converter", "reviewer")
        ],
    }


def test_transport_is_lossless_and_reasoning_is_not_returned_or_recorded(process):
    content = json.dumps({
        "workflow_yaml": 'env:\n  APP_ENV: "ci"\npath: "*.xml"\n',
        "unicode": "✓", "backslash": r"\build\test",
    }, ensure_ascii=False)
    private_events = [
        {"type": "assistant.reasoning", "data": {"content": "PRIVATE REASONING"}},
        {"type": "assistant.message", "data": {
            "phase": "analysis", "content": "PRIVATE ANALYSIS", "model": "other",
        }},
    ]
    process.stdout = "\n".join(map(json.dumps, private_events)) + "\n" + events(content)
    client = CopilotClient.create()
    assert client.complete("converter", "source") == content
    response = CopilotResponse.from_jsonl(process.stdout, CopilotClient.MODEL)
    assert response.content == content
    assert json.loads(response.content)["workflow_yaml"].endswith('path: "*.xml"\n')
    assert response.usage == {"premiumRequests": 1}
    assert "PRIVATE" not in json.dumps(client.metadata)


@pytest.mark.parametrize("model", ["gpt-6-sol", None, ""])
def test_model_substitution_is_an_explicit_error(process, model):
    process.stdout = events("{}", model)
    client = CopilotClient.create()
    with pytest.raises(BackendError, match="Requested gpt-6-luna"):
        client.complete("converter", "source")
    assert client.calls == []


@pytest.mark.parametrize("text", [
    "not JSON", "[]", "{}", '{"type": "assistant.message", "data": []}',
    json.dumps({"type": "result", "exitCode": 0}),
    events("{}").replace('"exitCode": 0', '"exitCode": 1'),
    events("{}").replace('"exitCode": 0', '"exitCode": false'),
    events("{}").replace('"content": "{}"', '"content": ""'),
    json.dumps({"type": "tool.execution_start", "data": {"toolName": "bash"}}),
    json.dumps({"type": "tool.execution_complete", "data": {}}),
    json.dumps({"type": "assistant.message", "data": {"toolRequests": [{"name": "bash"}]}}),
    json.dumps({"type": "session.error", "data": {"message": "Authentication failed"}}) + "\n" + events("{}"),
    json.dumps({"type": "assistant.message", "data": {"content": "{}", "model": "gpt-6-luna"}}),
    events("{}", "gpt-6-sol") + "\n" + events("{}"),
])
def test_bad_tool_using_or_incomplete_transport_is_rejected(text):
    with pytest.raises(BackendError):
        CopilotResponse.from_jsonl(text, CopilotClient.MODEL)


def test_missing_executable_is_explicit(monkeypatch):
    monkeypatch.setattr("copilot_client.client.shutil.which", lambda _: None)
    with pytest.raises(BackendError, match="not installed"):
        CopilotClient.create()


@pytest.mark.parametrize("timeout", [0, -1, True, False, 1j, "1", float("nan"), float("inf"), -float("inf")])
def test_timeout_is_rejected_before_executable_lookup(monkeypatch, timeout):
    def unexpected_lookup(_):
        pytest.fail("Invalid timeout must be rejected before executable discovery.")

    monkeypatch.setattr("copilot_client.client.shutil.which", unexpected_lookup)
    with pytest.raises(ValueError, match="finite and positive"):
        CopilotClient.create(timeout=timeout)


@pytest.mark.parametrize("role, prompt", [("other", "source"), ("converter", " \n")])
def test_invalid_calls_are_rejected_before_execution(process, role, prompt):
    client = CopilotClient.create()
    with pytest.raises(ValueError):
        client.complete(role, prompt)
    assert not process.timeouts
    assert not client.calls


def test_nonzero_exit_is_not_a_success_response(process):
    process.returncode = 1
    process.stderr = "Authentication failed"
    client = CopilotClient.create()
    with pytest.raises(BackendError, match="exited with 1: Authentication failed"):
        client.complete("converter", "source")
    assert client.calls == []


def test_empty_stdout_is_an_explicit_error(process):
    process.stdout = " \n"
    with pytest.raises(BackendError, match="empty response"):
        CopilotClient.create().complete("converter", "source")


def test_start_failure_is_an_explicit_error(process, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("Executable disappeared")

    monkeypatch.setattr("copilot_client.client.subprocess.Popen", fail)
    with pytest.raises(BackendError, match="Cannot start Copilot CLI"):
        CopilotClient.create().complete("converter", "source")


@pytest.mark.parametrize("group_already_exited", [False, True])
def test_timeout_terminates_only_the_specific_process_group(
    process, monkeypatch, group_already_exited,
):
    killed = []
    sigkill = object()

    def communicate(timeout=None):
        process.timeouts.append(timeout)
        if timeout is not None:
            raise subprocess.TimeoutExpired("copilot", timeout)
        return "", ""

    def killpg(pid, sig):
        killed.append((pid, sig))
        if group_already_exited:
            raise ProcessLookupError()

    process.communicate = communicate
    monkeypatch.setattr("copilot_client.client.os", SimpleNamespace(name="posix", killpg=killpg))
    monkeypatch.setattr("copilot_client.client.signal", SimpleNamespace(SIGKILL=sigkill))
    client = CopilotClient.create(timeout=0.25)
    with pytest.raises(BackendError, match="converter exceeded the 0.25-second call limit"):
        client.complete("converter", "source")
    assert killed == [(123, sigkill)]
    assert not process.killed
    assert process.timeouts == [0.25, None]
    assert client.calls == []


def test_timeout_kills_only_the_specific_process_without_posix_groups(process, monkeypatch):
    def communicate(timeout=None):
        process.timeouts.append(timeout)
        if timeout is not None:
            raise subprocess.TimeoutExpired("copilot", timeout)
        return "", ""

    process.communicate = communicate
    monkeypatch.setattr("copilot_client.client.os", SimpleNamespace(name="nt"))
    monkeypatch.setattr("copilot_client.client.signal", SimpleNamespace())
    with pytest.raises(BackendError, match="call limit"):
        CopilotClient.create(timeout=1).complete("reviewer", "candidate")
    assert process.killed
    assert process.timeouts == [1, None]
