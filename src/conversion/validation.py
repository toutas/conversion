from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Protocol

import yaml

from .models import Check


class Validator(Protocol):
    def check(self, path: Path) -> list[Check]: ...


class WorkflowError(ValueError):
    pass


class WorkflowLoader(yaml.BaseLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise yaml.constructor.ConstructorError(
                    None, None, f"Invalid or duplicate workflow key: {key}",
                    key_node.start_mark,
                )
            result[key] = self.construct_object(value_node, deep=deep)
        return result


@dataclass(frozen=True)
class Workflow:
    document: dict

    @classmethod
    def from_yaml(cls, text: str) -> "Workflow":
        document = yaml.load(text, Loader=WorkflowLoader)
        if not isinstance(document, dict):
            raise WorkflowError("The workflow must be a mapping.")
        if not isinstance(document.get("on"), (str, list, dict)) or not document["on"]:
            raise WorkflowError("A nonempty on trigger is required.")
        jobs = document.get("jobs")
        if not isinstance(jobs, dict) or not jobs:
            raise WorkflowError("jobs must be a nonempty mapping.")
        dependencies = {
            name: cls._check_job(name, job, jobs) for name, job in jobs.items()
        }
        cls._check_dependencies(dependencies)
        return cls(document)

    @classmethod
    def _check_job(cls, name: str, job: object, jobs: dict) -> list[str]:
        if not isinstance(job, dict):
            raise WorkflowError(f"jobs.{name} must be a mapping.")
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        if not isinstance(needs, list) or any(
            not isinstance(need, str) or need not in jobs for need in needs
        ):
            raise WorkflowError(f"jobs.{name}.needs references invalid jobs.")
        if "uses" in job:
            if not isinstance(job["uses"], str) or not job["uses"]:
                raise WorkflowError(f"jobs.{name}.uses must be a nonempty string.")
            if "steps" in job or "runs-on" in job:
                raise WorkflowError(f"jobs.{name} mixes a reusable workflow with steps.")
            return needs
        if not isinstance(job.get("runs-on"), (str, list, dict)) or not job["runs-on"]:
            raise WorkflowError(f"jobs.{name} needs runs-on.")
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            raise WorkflowError(f"jobs.{name}.steps must be a nonempty array.")
        for index, step in enumerate(steps):
            cls._check_step(f"jobs.{name}.steps[{index}]", step)
        return needs

    @staticmethod
    def _check_step(name: str, step: object) -> None:
        if not isinstance(step, dict):
            raise WorkflowError(f"{name} must be a mapping.")
        commands = [key for key in ("uses", "run") if key in step]
        if len(commands) != 1:
            raise WorkflowError(f"{name} needs exactly one of uses/run.")
        if not isinstance(step[commands[0]], str) or not step[commands[0]].strip():
            raise WorkflowError(f"{name} has an empty uses/run.")

    @staticmethod
    def _check_dependencies(dependencies: dict[str, list[str]]) -> None:
        remaining = dict(dependencies)
        while remaining:
            ready = [name for name, needs in remaining.items()
                     if not any(need in remaining for need in needs)]
            if not ready:
                raise WorkflowError("Job dependency graph contains a cycle.")
            for name in ready:
                del remaining[name]


class WorkflowValidator:
    def __init__(self, actionlint: str | None):
        self._actionlint = actionlint

    @classmethod
    def create(cls, require_actionlint: bool = False) -> "WorkflowValidator":
        executable = shutil.which("actionlint")
        if require_actionlint and executable is None:
            raise ValueError("--require-actionlint needs actionlint on PATH.")
        return cls(executable)

    @staticmethod
    def check_yaml(text: str) -> list[Check]:
        parsed = Check("yaml", "pass", "Parsed YAML; duplicate keys rejected.")
        try:
            Workflow.from_yaml(text)
        except yaml.YAMLError as exc:
            return [Check("yaml", "fail", str(exc))]
        except WorkflowError as exc:
            return [parsed, Check("workflow_structure", "fail", str(exc))]
        return [parsed, Check(
            "workflow_structure", "pass", "Basic job/step structure and dependencies checked.",
        )]

    def check(self, path: Path) -> list[Check]:
        checks = self.check_yaml(path.read_text(encoding="utf-8"))
        if self._actionlint is None:
            return checks + [Check("actionlint", "not_run", "actionlint is not installed.")]
        if any(check.status == "fail" for check in checks):
            return checks + [Check("actionlint", "not_run", "Basic YAML/structure checks failed.")]
        try:
            result = subprocess.run(
                [self._actionlint, "-no-color", str(path.resolve())],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return checks + [Check("actionlint", "fail", "actionlint exceeded 30 seconds.")]
        except OSError as exc:
            return checks + [Check("actionlint", "fail", f"Cannot run actionlint: {exc}")]
        detail = (result.stdout + result.stderr).strip() or f"actionlint exit code: {result.returncode}"
        return checks + [Check("actionlint", "pass" if result.returncode == 0 else "fail", detail)]
