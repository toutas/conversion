import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
from tempfile import TemporaryDirectory
from typing import Literal

from .response import BackendError, CopilotResponse


Role = Literal["converter", "reviewer"]


class CopilotClient:
    MODEL = "gpt-6-luna"
    REASONING_EFFORT = "high"

    def __init__(self, executable: str, timeout: float):
        if not executable:
            raise ValueError("Copilot executable must not be empty.")
        self._validate_timeout(timeout)
        self._executable = executable
        self.timeout = timeout
        self.calls: list[dict[str, object]] = []

    @classmethod
    def create(cls, timeout: float = 180) -> "CopilotClient":
        cls._validate_timeout(timeout)
        executable = shutil.which("copilot")
        if executable is None:
            raise BackendError("Copilot CLI is not installed or not on PATH.")
        return cls(executable, timeout)

    @staticmethod
    def _validate_timeout(timeout: float) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("Timeout must be finite and positive.")

    @property
    def metadata(self) -> dict[str, object]:
        return {
            "backend": "copilot_cli",
            "converter_model_requested": self.MODEL,
            "reviewer_model_requested": self.MODEL,
            "reasoning_effort_requested": self.REASONING_EFFORT,
            "timeout_per_call_seconds": self.timeout,
            "completed_calls": self.calls,
        }

    def complete(self, role: Role, prompt: str) -> str:
        if role not in ("converter", "reviewer") or not prompt.strip():
            raise ValueError("A valid role and nonempty prompt are required.")
        command = [
            self._executable, "--prompt", prompt,
            "--silent", "--stream=off", "--output-format=json", "--no-color",
            "--no-auto-update", "--no-custom-instructions", "--no-ask-user",
            "--disable-builtin-mcps", "--available-tools=",
            # Noninteractive grant; the empty tool filter and denials still apply.
            "--allow-all-tools",
            "--deny-tool=shell", "--deny-tool=write", "--deny-tool=url",
            "--model", self.MODEL, "--reasoning-effort", self.REASONING_EFFORT,
        ]
        with TemporaryDirectory(prefix=f".conversion-{role}-", dir=Path.cwd()) as cwd:
            stdout = self._execute(command, cwd, role)
        response = CopilotResponse.from_jsonl(stdout, self.MODEL)
        self.calls.append({"role": role, "model": response.model, "usage": response.usage})
        return response.content

    def _execute(self, command: list[str], cwd: str, role: Role) -> str:
        try:
            process = subprocess.Popen(
                command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise BackendError(f"Cannot start Copilot CLI: {exc}") from exc
        try:
            stdout, stderr = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # The timed-out process group already exited.
            else:
                process.kill()
            process.communicate()
            raise BackendError(
                f"{role} exceeded the {self.timeout:g}-second call limit."
            ) from exc
        if process.returncode != 0:
            raise BackendError(
                f"Copilot {role} exited with {process.returncode}: {stderr.strip()}"
            )
        if not stdout.strip():
            raise BackendError(f"Copilot {role} returned an empty response.")
        return stdout
