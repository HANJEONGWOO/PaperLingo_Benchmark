"""Automatic scope selection keeps fixed scoring denominators and validation."""
import json

from fastapi.testclient import TestClient
import pytest

from paperlingo.app import create_app
from paperlingo.config import Settings

HEADERS = {"X-Requested-With": "PaperLingo"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        yield client


def submit(client, predictions, **fields):
    return client.post("/api/runs", headers=HEADERS, json={
        "name": "자동 범위 검사", "predictions": predictions, **fields,
    })


@pytest.mark.parametrize("encoding", ["array", "json", "jsonl"])
def test_example_answers_auto_select_demo_and_score_full_marks(client, encoding):
    predictions = client.get("/api/sample").json()["predictions"]
    if encoding == "json":
        predictions = json.dumps(predictions)
    elif encoding == "jsonl":
        predictions = "\n".join(json.dumps(row) for row in predictions)
    response = submit(client, predictions)
    assert response.status_code == 201, response.text
    report = response.json()["report"]
    assert report["scope"]["name"] == "demo"
    assert report["scope"]["paper_count"] == 2
    assert report["counts"]["gold"] == 9
    assert report["metrics"]["final_score"] == 100


@pytest.mark.parametrize("kind", ["partial_demo", "partial_evaluation", "empty"])
def test_automatic_scope_never_shrinks_to_submitted_papers(client, kind):
    dataset = client.app.state.dataset
    scope = "demo" if kind == "partial_demo" else "evaluation"
    predictions = [] if kind == "empty" else [{"paper_id": dataset.splits[scope][0], "questions": []}]
    response = submit(client, predictions)
    assert response.status_code == 201, response.text
    report = response.json()["report"]
    assert report["scope"]["name"] == scope
    assert report["scope"]["paper_ids"] == dataset.splits[scope]
    assert report["counts"]["missing_papers"] == len(dataset.splits[scope]) - len(predictions)
    assert report["counts"]["gold"] == sum(len(dataset.labels[pid]) for pid in dataset.splits[scope])
    assert report["metrics"]["final_score"] == 0


def test_mixed_scopes_and_caller_shortened_scope_are_rejected(client):
    dataset = client.app.state.dataset
    demo = {"paper_id": dataset.splits["demo"][0], "questions": []}
    evaluation = {"paper_id": dataset.splits["evaluation"][0], "questions": []}
    mixed = submit(client, [demo, evaluation])
    assert mixed.status_code == 422
    assert "각각 별도 파일로 제출" in mixed.json()["detail"]
    assert submit(client, [evaluation], paper_ids=[evaluation["paper_id"]]).status_code == 422
    assert client.get("/api/runs").json()["items"] == []


@pytest.mark.parametrize("paper_id", ["unknown-paper", None, [], {}, 123])
def test_bad_ids_are_rejected_instead_of_dropped_or_crashing(client, paper_id):
    response = submit(client, [{"paper_id": paper_id, "questions": []}])
    assert response.status_code == 422
    assert client.get("/api/runs").json()["items"] == []


def test_excluded_papers_remain_outside_automatic_scope(client):
    dataset = client.app.state.dataset
    excluded = sorted(set(dataset.papers) - set(dataset.splits["demo"]) - set(dataset.splits["evaluation"]))[0]
    response = submit(client, [{"paper_id": excluded, "questions": []}])
    assert response.status_code == 422
    assert excluded in response.text
    assert client.get("/api/runs").json()["items"] == []
