from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


@pytest.fixture
def workspace():
    with TemporaryDirectory(prefix=".test-", dir=Path(__file__).parent) as directory:
        yield Path(directory)


@pytest.fixture
def workflow():
    return """name: Example
on:
  workflow_dispatch:
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ./gradlew --no-daemon clean build
"""


@pytest.fixture
def conversion_data(workflow):
    return {
        "workflow_yaml": workflow,
        "assumptions": ["Manual dispatch; Jenkins trigger is unknown."],
        "behavior_mapping": [{"source": "Build", "target": "jobs.build"}],
        "disputes": [],
    }


@pytest.fixture
def review_data():
    return {
        "verdict": "request_changes",
        "findings": [{
            "id": "missing-tests",
            "severity": "blocker",
            "message": "Tests missing.",
            "source_evidence": "stage('Test')",
            "requested_change": "Add the test command.",
        }],
        "limitations": ["No CI execution was performed."],
    }
