# conversion

Convert a Jenkinsfile into a GitHub Actions workflow using a Copilot-powered
conversion/review loop.

## Usage

Requires [uv](https://docs.astral.sh/uv/), and an authenticated
`copilot` CLI. Default is `gpt-6-luna` model.

From the repository root:

```bash
uv sync

uv run python -m conversion tests/fixtures/basic.Jenkinsfile --output evaluations/basic --max-rounds 3 --timeout 360

uv run python -m conversion tests/fixtures/container-parallel.Jenkinsfile --output evaluations/container-parallel --max-rounds 3 --timeout 360
```

with `actionlint` workflows are linted automatically.
add `--require-actionlint` to fail early when it is unavailable.

Each output directory contains `report.json`, `backend.json`, and per-round conversion, validation, and review artifacts. An accepted conversion also writes the `workflow.yml` in that directory.