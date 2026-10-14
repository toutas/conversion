import json

import pytest

from conversion.responses import Conversion, ResponseError, Review, Severity, Verdict


def test_conversion_factory_validates_and_serializes_fields(conversion_data):
    conversion_data["disputes"] = [{
        "finding_id": "finding-1", "source_evidence": "stage('Build')",
    }]
    conversion = Conversion.from_json(json.dumps(conversion_data))
    assert conversion.to_dict() == conversion_data
    assert conversion.behavior_mapping[0].source == "Build"
    assert conversion.disputes[0].finding_id == "finding-1"


@pytest.mark.parametrize("text", [
    "", "not JSON", "```json\n{}\n```", "[]", "null",
    '{"workflow_yaml": "one", "workflow_yaml": "two"}',
])
def test_conversion_rejects_malformed_responses(text):
    with pytest.raises(ResponseError):
        Conversion.from_json(text)


@pytest.mark.parametrize("field, value", [
    ("workflow_yaml", ""), ("workflow_yaml", " \n"), ("workflow_yaml", 42),
    ("assumptions", None), ("assumptions", "assumption"), ("assumptions", [1]),
    ("behavior_mapping", []), ("behavior_mapping", [{}]),
    ("behavior_mapping", [{"source": "Build", "target": ""}]),
    ("behavior_mapping", ["Build"]),
    ("disputes", None), ("disputes", [{"finding_id": "x", "source_evidence": ""}]),
])
def test_conversion_requires_well_formed_fields(conversion_data, field, value):
    conversion_data[field] = value
    with pytest.raises(ResponseError):
        Conversion.from_json(json.dumps(conversion_data))


@pytest.mark.parametrize("field", ["workflow_yaml", "assumptions", "behavior_mapping", "disputes"])
def test_conversion_requires_all_fields(conversion_data, field):
    del conversion_data[field]
    with pytest.raises(ResponseError):
        Conversion.from_json(json.dumps(conversion_data))


def test_review_factory_validates_and_serializes_findings(review_data):
    review = Review.from_json(json.dumps(review_data))
    assert review.verdict == Verdict.REQUEST_CHANGES
    assert review.findings[0].severity == Severity.BLOCKER
    assert review.to_dict() == review_data


@pytest.mark.parametrize("verdict, severity", [
    ("approve", "blocker"),
    ("request_changes", "advisory"),
    ("blocked", "advisory"),
])
def test_review_rejects_contradictory_verdicts(review_data, verdict, severity):
    review_data["verdict"] = verdict
    review_data["findings"][0]["severity"] = severity
    with pytest.raises(ResponseError):
        Review.from_json(json.dumps(review_data))


@pytest.mark.parametrize("verdict", ["request_changes", "blocked"])
def test_rejection_requires_a_blocker(review_data, verdict):
    review_data.update(verdict=verdict, findings=[])
    with pytest.raises(ResponseError):
        Review.from_json(json.dumps(review_data))


def test_duplicate_finding_ids_are_rejected(review_data):
    review_data["findings"].append({
        **review_data["findings"][0], "message": "A different issue with the same ID.",
    })
    with pytest.raises(ResponseError, match="IDs must be unique"):
        Review.from_json(json.dumps(review_data))


@pytest.mark.parametrize("field, value", [
    ("verdict", "accept"), ("verdict", None),
    ("findings", None), ("findings", [{}]), ("limitations", "unknown"),
])
def test_review_rejects_malformed_fields(review_data, field, value):
    review_data[field] = value
    with pytest.raises(ResponseError):
        Review.from_json(json.dumps(review_data))


@pytest.mark.parametrize("field, value", [
    ("id", ""), ("severity", "nitpick"), ("message", 1),
    ("source_evidence", ""), ("requested_change", ""),
])
def test_findings_require_material_evidence_and_known_severity(review_data, field, value):
    review_data["findings"][0][field] = value
    with pytest.raises(ResponseError):
        Review.from_json(json.dumps(review_data))


def test_duplicate_json_fields_cannot_hide_a_contradictory_verdict(review_data):
    text = json.dumps(review_data).replace(
        '"verdict": "request_changes"', '"verdict": "approve", "verdict": "request_changes"',
    )
    with pytest.raises(ResponseError, match="Duplicate JSON field: verdict"):
        Review.from_json(text)
