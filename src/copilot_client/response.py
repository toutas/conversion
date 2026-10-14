from dataclasses import dataclass
import json


class BackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class CopilotResponse:
    content: str
    model: str
    usage: object

    @classmethod
    def from_jsonl(cls, stdout: str, expected_model: str) -> "CopilotResponse":
        content = None
        usage = None
        completed = False
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BackendError(f"Invalid Copilot JSONL transport: {exc}") from exc
            if not isinstance(event, dict):
                raise BackendError("Copilot JSONL events must be objects.")
            event_type = event.get("type")
            if not isinstance(event_type, str) or not event_type:
                raise BackendError("Copilot JSONL events must have a type.")
            if event_type.startswith("tool."):
                raise BackendError("A supposedly tool-free agent attempted tool execution.")
            if event_type in ("error", "session.error"):
                raise BackendError(f"Copilot reported an error: {event.get('data')}")
            if event_type == "assistant.message":
                data = event.get("data")
                if not isinstance(data, dict):
                    raise BackendError("Malformed Copilot assistant.message event.")
                if data.get("toolRequests"):
                    raise BackendError("A tool-free agent returned tool requests.")
                if data.get("phase") in (None, "final_answer"):
                    content, model = data.get("content"), data.get("model")
                    if model != expected_model:
                        raise BackendError(
                            f"Requested {expected_model}, but Copilot reported {model!r}."
                        )
            if event_type == "result":
                if type(event.get("exitCode")) is not int or event["exitCode"] != 0:
                    raise BackendError(f"Copilot result has exitCode {event.get('exitCode')}.")
                completed, usage = True, event.get("usage")
        if not completed or not isinstance(content, str) or not content.strip():
            raise BackendError("Copilot JSONL lacks a completed assistant response.")
        return cls(content, expected_model, usage)
