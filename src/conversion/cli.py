import argparse
import math
from pathlib import Path
import sys

from copilot_client import BackendError, CopilotClient

from .loop import ConversionLoop
from .models import ASSURANCE, Status
from .validation import WorkflowValidator


class CommandLine:
    @staticmethod
    def _positive_int(value: str) -> int:
        result = int(value)
        if result < 1:
            raise argparse.ArgumentTypeError("Must be positive.")
        return result

    @staticmethod
    def _positive_seconds(value: str) -> float:
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise argparse.ArgumentTypeError("Must be finite and positive.")
        return result

    @classmethod
    def run(cls, argv: list[str] | None = None) -> int:
        parser = argparse.ArgumentParser(description="Source-only Jenkinsfile conversion/review loop.")
        parser.add_argument("jenkinsfile", type=Path)
        parser.add_argument("--output", required=True, type=Path, help="A new artifact directory.")
        parser.add_argument("--max-rounds", type=cls._positive_int, default=3)
        parser.add_argument("--timeout", type=cls._positive_seconds, default=180,
                            help="Seconds per LLM call.")
        # parser.add_argument("--converter-model", default=CopilotClient.MODEL)
        # parser.add_argument("--reviewer-model", default=CopilotClient.MODEL)
        # parser.add_argument(
        #     "--reasoning-effort",
        #     choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
        #     default=CopilotClient.REASONING_EFFORT,
        #     help="Reasoning effort for both agent roles.",
        # )
        parser.add_argument("--require-actionlint", action="store_true")
        args = parser.parse_args(argv)
        try:
            source = args.jenkinsfile.read_bytes().decode("utf-8")
            validator = WorkflowValidator.create(args.require_actionlint)
            client = CopilotClient.create(args.timeout)
            loop = ConversionLoop.create(source, args.output, client, args.max_rounds, validator)
            report = loop.run()
            loop.artifacts.write_json("backend.json", {
                **client.metadata, "max_rounds": args.max_rounds,
            })
        except (OSError, UnicodeError, ValueError, BackendError) as exc:
            print(f"Conversion error: {exc}", file=sys.stderr)
            return 2
        print(f"{report.status}: {args.output / 'report.json'}")
        print(ASSURANCE)
        if report.error:
            print(report.error, file=sys.stderr)
        return 0 if report.status == Status.ACCEPTED else 1
