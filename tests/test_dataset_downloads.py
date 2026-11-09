"""Fixed training/evaluation scopes and complete, aligned public downloads."""

import json

from fastapi.testclient import TestClient
import pytest

from paperlingo.app import create_app
from paperlingo.config import Settings
from paperlingo.scoring import evaluate


HEADERS = {"X-Requested-With": "PaperLingo"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        yield client


def read_download(client, scope, kind):
    response = client.get(f"/api/datasets/{scope}/{kind}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-dataset-version"] == client.app.state.dataset.version
    assert response.headers["x-split-version"] == "train-evaluation-v1"
    return [json.loads(line) for line in response.text.splitlines()]


@pytest.mark.parametrize("scope, count", [("train", 1043), ("evaluation", 447)])
def test_papers_and_labels_are_aligned_complete_source_subsets(client, scope, count):
    dataset = client.app.state.dataset
    papers = read_download(client, scope, "papers")
    labels = read_download(client, scope, "labels")
    ids = dataset.splits[scope]
    assert len(papers) == len(labels) == count
    assert [row["paper_id"] for row in papers] == [row["paper_id"] for row in labels] == ids
    for paper, label in zip(papers, labels):
        original = dataset.papers[paper["paper_id"]]
        assert paper["abstract"] == original["abstract"]
        assert paper["paper_name"] == original["paper_name"]
        assert "expressions" not in paper
        assert "abstract_original" not in paper
        assert label == {"paper_id": paper["paper_id"], "expressions": dataset.labels[paper["paper_id"]]}
    # Labels in the evaluation split are deliberately public, as requested.
    assert any(row["expressions"] for row in labels)


def test_training_answers_cover_all_training_labels_and_are_valid_submissions(client):
    dataset = client.app.state.dataset
    answers = read_download(client, "train", "answers")
    assert [row["paper_id"] for row in answers] == dataset.splits["train"]
    for row in answers:
        assert set(row) == {"paper_id", "questions"}
        assert len(row["questions"]) <= 5
        assert {q["expression_text"] for q in row["questions"]} == {e["text"] for e in dataset.labels[row["paper_id"]]}
        for question in row["questions"]:
            assert set(question) == {"expression_text", "options", "answer_id"}
            assert len(set(question["options"])) == 4
            assert type(question["answer_id"]) is int and 0 <= question["answer_id"] <= 3
    report = evaluate(dataset, answers, dataset.splits["train"], "train")
    assert report["metrics"]["recall_pct"] == 100
    assert report["metrics"]["structural_validity_pct"] == 100
    assert report["metrics"]["final_score"] is None  # No invented quality validation.
    headers = client.get("/api/datasets/train/answers").headers
    assert headers["x-answer-version"] == "rule-based-v1"
    assert headers["x-answer-quality"] == "unreviewed"
    assert client.get("/api/datasets/evaluation/answers").status_code == 404
    assert client.get("/api/runs").json()["items"] == []  # Downloading never starts inference.


def test_manifest_and_explorer_use_only_new_scopes(client):
    manifest = client.get("/api/splits?download=1").json()
    assert manifest["split_version"] == "train-evaluation-v1"
    assert manifest["training_answers"]["quality_status"] == "unreviewed"
    assert set(manifest["splits"]) == {"demo", "train", "evaluation"}
    assert set(manifest["splits"]["demo"]) <= set(manifest["splits"]["train"])
    assert not set(manifest["splits"]["train"]) & set(manifest["splits"]["evaluation"])
    assert client.get("/api/papers").json()["total"] == 1043
    for scope, count in (("train", 1043), ("evaluation", 447)):
        response = client.get("/api/papers", params={"scope": scope, "limit": 100, "offset": count - 3}).json()
        assert response["total"] == count
        assert len(response["items"]) == 3
        assert all(row["paper_id"] in manifest["splits"][scope] for row in response["items"])
        pid = response["items"][0]["paper_id"]
        assert "expressions" in client.get(f"/api/papers/{pid}").json()
    assert client.get("/api/papers?scope=all").status_code == 422


@pytest.mark.parametrize("scope", ["development", "validation", "test", "all", "custom"])
def test_retired_scopes_cannot_create_new_runs(client, scope):
    response = client.post("/api/runs", headers=HEADERS, json={"name": "old scope", "scope": scope, "predictions": []})
    assert response.status_code == 422
    assert client.get("/api/runs").json()["items"] == []


def test_evaluation_scope_rejects_training_papers_and_cannot_shrink_denominator(client):
    dataset = client.app.state.dataset
    body = {"name": "scope boundary", "scope": "evaluation", "predictions": []}
    body["paper_ids"] = dataset.splits["evaluation"][:1]
    assert client.post("/api/runs", headers=HEADERS, json=body).status_code == 422
    body.pop("paper_ids")
    body["predictions"] = [{"paper_id": dataset.splits["train"][0], "questions": []}]
    assert client.post("/api/runs", headers=HEADERS, json=body).status_code == 422
    body["predictions"] = []
    result = client.post("/api/runs", headers=HEADERS, json=body)
    assert result.status_code == 201, result.text
    report = result.json()["report"]
    assert report["scope"]["paper_count"] == report["counts"]["missing_papers"] == 447
    assert report["counts"]["gold"] == sum(len(dataset.labels[pid]) for pid in dataset.splits["evaluation"])
    assert report["split_version"] == "train-evaluation-v1"
    assert report["metrics"]["final_score"] == 0


@pytest.mark.parametrize("path", ["all/papers", "custom/labels", "evaluation/unknown", "train/../../.env"])
def test_downloads_accept_only_named_public_resources(client, path):
    assert client.get("/api/datasets/" + path).status_code in {404, 422}
