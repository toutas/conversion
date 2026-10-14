import json
from pathlib import Path


class Artifacts:
    def __init__(self, root: Path):
        self.root = root

    @classmethod
    def create(cls, root: Path, source: str) -> "Artifacts":
        if not source.strip():
            raise ValueError("Jenkinsfile must not be empty.")
        root.mkdir(parents=True, exist_ok=False)
        artifacts = cls(root)
        artifacts.write_text("input.Jenkinsfile", source)
        return artifacts

    def create_round(self, number: int) -> Path:
        name = Path(f"round-{number:02}")
        self.path(name).mkdir()
        return name

    def path(self, name: str | Path) -> Path:
        name = Path(name)
        if name.anchor or ".." in name.parts:
            raise ValueError("Artifact names must stay within the run directory.")
        return self.root / name

    def write_text(self, name: str | Path, text: str) -> Path:
        path = self.path(name)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def write_json(self, name: str | Path, data: object) -> None:
        self.write_text(name, json.dumps(data, indent=2, ensure_ascii=True) + "\n")
