import subprocess

import pytest
import yaml

from conversion.validation import Workflow, WorkflowError, WorkflowValidator


def test_yaml_on_and_null_manual_trigger_are_preserved(workflow):
    parsed = Workflow.from_yaml(workflow)
    assert parsed.document["on"] == {"workflow_dispatch": ""}
    assert True not in parsed.document


def test_reusable_workflow_jobs_are_valid():
    workflow = Workflow.from_yaml("""on: workflow_dispatch
jobs:
  build:
    uses: ./.github/workflows/reusable.yml
  release:
    needs: [build]
    uses: org/repo/.github/workflows/release.yml@main
""")
    assert workflow.document["jobs"]["release"]["needs"] == ["build"]


@pytest.mark.parametrize("replacement", [
    "    runs-on: [ubuntu-latest]",
    "    runs-on:\n      group: build-runners\n      labels: linux",
])
def test_runner_label_variants_are_valid(workflow, replacement):
    Workflow.from_yaml(workflow.replace("    runs-on: ubuntu-latest", replacement))


@pytest.mark.parametrize("change", [
    lambda text: text.replace("on:", "true:", 1),
    lambda text: text.replace("  workflow_dispatch:", ""),
    lambda text: text.replace("jobs:\n", "jobs: []\nignored:\n"),
    lambda text: text.replace("    runs-on: ubuntu-latest\n", ""),
    lambda text: text.replace("    steps:", "    needs: missing\n    steps:"),
    lambda text: text.replace("    steps:", "    needs: build\n    steps:"),
    lambda text: text.replace("    steps:", "    needs: [42]\n    steps:"),
    lambda text: text.replace("    steps:", "    needs: {build: yes}\n    steps:"),
    lambda text: text.replace("      - uses: actions/checkout@v4", "      - []"),
    lambda text: text.replace("      - uses: actions/checkout@v4", "      - name: No command"),
    lambda text: text.replace("      - uses: actions/checkout@v4", "      - run: ''"),
    lambda text: text.replace(
        "      - uses: actions/checkout@v4", "      - uses: actions/checkout@v4\n        run: echo bad",
    ),
    lambda text: text.replace("    runs-on: ubuntu-latest", "    uses: ./reusable.yml"),
])
def test_workflow_factory_rejects_invalid_structure(workflow, change):
    with pytest.raises(WorkflowError):
        Workflow.from_yaml(change(workflow))


@pytest.mark.parametrize("text", [
    "[]", "jobs: {}", "on: workflow_dispatch\njobs: {}",
    "on: workflow_dispatch\njobs:\n  build: []",
    "on: workflow_dispatch\njobs:\n  build:\n    uses: ''",
    "on: workflow_dispatch\njobs:\n  build:\n    runs-on: linux\n    steps: []",
    """on: workflow_dispatch
jobs:
  first:
    needs: second
    uses: ./first.yml
  second:
    needs: third
    uses: ./second.yml
  third:
    needs: first
    uses: ./third.yml
""",
])
def test_missing_jobs_and_dependency_cycles_are_rejected(text):
    with pytest.raises(WorkflowError):
        Workflow.from_yaml(text)


@pytest.mark.parametrize("duplicate", [
    "jobs: {}\n",
    "name: Duplicate\n",
    "env:\n  APP_ENV: ci\n  APP_ENV: production\n",
])
def test_duplicate_keys_are_rejected_at_every_depth(workflow, duplicate):
    with pytest.raises(yaml.YAMLError, match="duplicate"):
        Workflow.from_yaml(workflow + duplicate)


def test_missing_required_actionlint_fails_during_factory(monkeypatch):
    monkeypatch.setattr("conversion.validation.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="needs actionlint"):
        WorkflowValidator.create(require_actionlint=True)


def test_optional_actionlint_is_reported_not_run(workspace, workflow, monkeypatch):
    monkeypatch.setattr("conversion.validation.shutil.which", lambda _: None)
    path = workspace / "workflow.yml"
    path.write_text(workflow)
    checks = WorkflowValidator.create().check(path)
    assert [(check.name, check.status) for check in checks] == [
        ("yaml", "pass"), ("workflow_structure", "pass"), ("actionlint", "not_run"),
    ]


@pytest.mark.parametrize("text, failed_check", [
    ("jobs: [", "yaml"), ("on: workflow_dispatch\njobs: {}", "workflow_structure"),
])
def test_basic_failures_skip_actionlint(workspace, monkeypatch, text, failed_check):
    def unexpected_run(*args, **kwargs):
        pytest.fail("Basic static failures must skip actionlint.")

    monkeypatch.setattr("conversion.validation.subprocess.run", unexpected_run)
    path = workspace / "workflow.yml"
    path.write_text(text)
    checks = WorkflowValidator("/fake/actionlint").check(path)
    assert next(check.status for check in checks if check.name == failed_check) == "fail"
    assert checks[-1].status == "not_run"


@pytest.mark.parametrize("failure, detail", [
    (OSError("Unavailable"), "Cannot run actionlint"),
    (subprocess.TimeoutExpired("actionlint", 30), "exceeded 30 seconds"),
])
def test_actionlint_failures_remain_failures(workspace, workflow, monkeypatch, failure, detail):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr("conversion.validation.subprocess.run", fail)
    path = workspace / "workflow.yml"
    path.write_text(workflow)
    check = WorkflowValidator("/fake/actionlint").check(path)[-1]
    assert check.status == "fail" and detail in check.detail


@pytest.mark.parametrize("returncode, status", [(0, "pass"), (1, "fail")])
def test_actionlint_results_are_recorded(workspace, workflow, monkeypatch, returncode, status):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode, "diagnostic\n", "error\n")

    monkeypatch.setattr("conversion.validation.subprocess.run", run)
    path = workspace / "workflow.yml"
    path.write_text(workflow)
    check = WorkflowValidator("/fake/actionlint").check(path)[-1]
    assert check.status == status and check.detail == "diagnostic\nerror"
    assert calls == [(
        ["/fake/actionlint", "-no-color", str(path.resolve())],
        {"capture_output": True, "text": True, "timeout": 30},
    )]
