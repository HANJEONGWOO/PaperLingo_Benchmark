"""Aligned example downloads and held-out evaluation references."""

import json

from fastapi.testclient import TestClient
import pytest

from paperlingo.app import create_app
from paperlingo.config import Settings
from paperlingo.scoring import FUNCTION_WORDS, evaluate


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
    assert response.headers["x-split-version"] == "evaluation-min5sentences-v3"
    return [json.loads(line) for line in response.text.splitlines()]


@pytest.mark.parametrize("scope, count", [("demo", 3), ("evaluation", 1044)])
def test_papers_are_complete_source_subsets_without_labels(client, scope, count):
    dataset = client.app.state.dataset
    papers = read_download(client, scope, "papers")
    ids = dataset.splits[scope]
    assert len(papers) == count
    assert [row["paper_id"] for row in papers] == ids
    for paper in papers:
        original = dataset.papers[paper["paper_id"]]
        assert paper["abstract"] == original["abstract"]
        assert paper["paper_name"] == original["paper_name"]
        assert "expressions" not in paper
        assert "abstract_original" not in paper


def test_example_labels_remain_complete_and_aligned(client):
    dataset = client.app.state.dataset
    labels = read_download(client, "demo", "labels")
    assert [row["paper_id"] for row in labels] == dataset.splits["demo"]
    for label in labels:
        assert label == {"paper_id": label["paper_id"], "expressions": dataset.labels[label["paper_id"]]}
    assert any(row["expressions"] for row in labels)


@pytest.mark.parametrize("kind", ["labels", "answers"])
@pytest.mark.parametrize("query", ["", "?scope=train&download=1"])
def test_evaluation_reference_downloads_cannot_be_requested_directly(client, kind, query):
    response = client.get(f"/api/datasets/evaluation/{kind}{query}")
    assert response.status_code == 404
    assert "content-disposition" not in response.headers


def test_example_answers_cover_all_example_labels_and_are_valid_submissions(client):
    dataset = client.app.state.dataset
    answers = read_download(client, "demo", "answers")
    assert answers == client.get("/api/sample").json()["predictions"]
    assert [row["paper_id"] for row in answers] == dataset.splits["demo"]
    for row in answers:
        assert set(row) == {"paper_id", "questions"}
        assert len(row["questions"]) <= 5
        assert {q["expression_text"] for q in row["questions"]} == {e["text"] for e in dataset.labels[row["paper_id"]]}
        for question in row["questions"]:
            assert set(question) == {"expression_text", "options", "answer_id"}
            assert len(set(question["options"])) == 4
            assert type(question["answer_id"]) is int and 0 <= question["answer_id"] <= 3
    report = evaluate(dataset, answers, dataset.splits["demo"], "demo")
    assert report["metrics"]["recall_pct"] == 100
    assert report["metrics"]["structural_validity_pct"] == 100
    assert report["metrics"]["final_score"] is None  # No invented quality validation.
    headers = client.get("/api/datasets/demo/answers").headers
    assert headers["x-answer-version"] == "authored-demo-v1"
    assert headers["x-answer-quality"] == "code-checked"
    assert client.get("/api/datasets/evaluation/answers").status_code == 404
    assert client.get("/api/runs").json()["items"] == []  # Downloading never starts inference.


def test_manifest_and_explorer_use_only_new_scopes(client):
    manifest = client.get("/api/splits?download=1").json()
    assert manifest["split_version"] == "evaluation-min5sentences-v3"
    assert manifest["example_answers"]["quality_status"] == "code-checked"
    assert manifest["example_answers"]["semantic_reviewed"] is False
    assert manifest["evaluation_filter"]["minimum_sentences"] == 5
    assert manifest["evaluation_filter"]["excluded_paper_count"] == 443
    assert set(manifest["evaluation_sentence_counts"]) == set(manifest["splits"]["evaluation"])
    assert all(count >= 5 for count in manifest["evaluation_sentence_counts"].values())
    assert client.get("/api/schema").json()["scopes"] == ["demo", "evaluation"]
    assert set(manifest["splits"]) == {"demo", "evaluation"}
    assert len(manifest["splits"]["demo"]) == 3
    assert not set(manifest["splits"]["demo"]) & set(manifest["splits"]["evaluation"])
    assert client.get("/api/papers").json()["total"] == 3
    for scope, count in (("demo", 3), ("evaluation", 1044)):
        response = client.get("/api/papers", params={"scope": scope, "limit": 100, "offset": count - 3}).json()
        assert response["total"] == count
        assert len(response["items"]) == 3
        assert all(row["paper_id"] in manifest["splits"][scope] for row in response["items"])
        pid = response["items"][0]["paper_id"]
        detail = client.get(f"/api/papers/{pid}").json()
        assert ("expressions" in detail) == (scope == "demo")
        assert all(("gold_count" in row) == (scope == "demo") for row in response["items"])
    assert client.get("/api/papers?scope=all").status_code == 422
    assert client.get("/api/papers?scope=train").status_code == 422


@pytest.mark.parametrize("kind", ["papers", "labels", "answers"])
def test_retired_training_downloads_do_not_expose_moved_papers(client, kind):
    response = client.get(f"/api/datasets/train/{kind}")
    assert response.status_code == 422
    assert "content-disposition" not in response.headers


def test_former_training_paper_is_now_private_evaluation_data(client):
    # This paper was in the previously published 70% training partition.
    paper_id = "2208.09462"
    assert paper_id in client.app.state.dataset.splits["evaluation"]
    for query in ("", "?scope=train", "?scope=demo"):
        detail = client.get(f"/api/papers/{paper_id}{query}").json()
        assert detail["reference_hidden"] is True
        assert "expressions" not in detail


def test_short_abstract_is_excluded_but_its_references_remain_private(client):
    dataset = client.app.state.dataset
    paper_id = "math/0404367"
    assert paper_id not in dataset.splits["evaluation"]
    assert paper_id not in dataset.splits["demo"]
    for query in ("", "?scope=demo", "?scope=evaluation"):
        detail = client.get(f"/api/papers/{paper_id}{query}").json()
        assert detail["reference_hidden"] is True
        assert "expressions" not in detail
    response = client.post("/api/runs", headers=HEADERS, json={
        "name": "excluded short abstract", "scope": "evaluation",
        "predictions": [{"paper_id": paper_id, "questions": []}],
    })
    assert response.status_code == 422
    assert client.get("/api/runs").json()["items"] == []
    report = evaluate(dataset, [], [paper_id], "evaluation")
    report["split_version"] = "example-evaluation-v2"
    stored = client.app.state.store.create("older evaluation", report)
    for suffix in ("", "/export"):
        visible = client.get(f'/api/runs/{stored["id"]}{suffix}').json()["report"]
        assert_hidden(visible["papers"][0], dataset)
        assert visible["metrics"] == report["metrics"]
        assert visible["scope"] == report["scope"]
        assert visible["split_version"] == "example-evaluation-v2"
    assert client.app.state.store.get(stored["id"]) == stored


@pytest.mark.parametrize("scope", ["train", "development", "validation", "test", "all", "custom"])
def test_retired_scopes_cannot_create_new_runs(client, scope):
    response = client.post("/api/runs", headers=HEADERS, json={"name": "old scope", "scope": scope, "predictions": []})
    assert response.status_code == 422
    assert client.get("/api/runs").json()["items"] == []


def test_evaluation_scope_rejects_example_papers_and_cannot_shrink_denominator(client):
    dataset = client.app.state.dataset
    body = {"name": "scope boundary", "scope": "evaluation", "predictions": []}
    body["paper_ids"] = dataset.splits["evaluation"][:1]
    assert client.post("/api/runs", headers=HEADERS, json=body).status_code == 422
    body.pop("paper_ids")
    body["predictions"] = [{"paper_id": dataset.splits["demo"][0], "questions": []}]
    assert client.post("/api/runs", headers=HEADERS, json=body).status_code == 422
    body["predictions"] = []
    result = client.post("/api/runs", headers=HEADERS, json=body)
    assert result.status_code == 201, result.text
    report = result.json()["report"]
    assert report["scope"]["paper_count"] == report["counts"]["missing_papers"] == 1044
    assert report["counts"]["gold"] == sum(len(dataset.labels[pid]) for pid in dataset.splits["evaluation"])
    assert report["split_version"] == "evaluation-min5sentences-v3"
    assert report["metrics"]["final_score"] == 0


@pytest.mark.parametrize("query", ["", "?scope=train", "?scope=demo", "?scope=all"])
def test_paper_detail_hides_evaluation_labels_based_on_server_membership(client, query):
    dataset = client.app.state.dataset
    # Include slash-containing IDs to cover path handling if present in this split.
    ids = [dataset.splits["evaluation"][0]]
    ids.extend(pid for pid in dataset.splits["evaluation"] if "/" in pid)
    for pid in ids:
        detail = client.get(f"/api/papers/{pid}{query}").json()
        assert detail["paper_id"] == pid
        assert detail["abstract"] == dataset.papers[pid]["abstract"]
        assert detail["reference_hidden"] is True
        assert "expressions" not in detail


def assert_hidden(paper, dataset):
    assert paper["reference_hidden"] is True
    assert "gold_expressions" not in paper and "missing_expressions" not in paper
    assert paper["gold_count"] == len(dataset.labels[paper["paper_id"]])


def test_empty_submission_cannot_download_evaluation_answer_key(client):
    dataset = client.app.state.dataset
    response = client.post("/api/runs", headers=HEADERS, json={
        "name": "empty evaluation", "scope": "evaluation", "predictions": [],
    })
    assert response.status_code == 201
    run = response.json()
    for endpoint in (None, f'/api/runs/{run["id"]}', f'/api/runs/{run["id"]}/export'):
        report = run["report"] if endpoint is None else client.get(endpoint).json()["report"]
        assert report["metrics"]["final_score"] == 0
        for paper in report["papers"]:
            assert_hidden(paper, dataset)
            assert paper["missing_count"] == paper["gold_count"]
    stored = client.app.state.store.get(run["id"])
    assert all("gold_expressions" in paper for paper in stored["report"]["papers"])
    assert "reference_hidden" not in stored["report"]["papers"][0]


def test_evaluation_scoring_still_uses_private_reference_labels(client):
    dataset = client.app.state.dataset
    pid = next(pid for pid in dataset.splits["evaluation"] if dataset.labels[pid])
    expression = dataset.labels[pid][0]["text"]
    question = {"expression_text": expression, "options": [expression, "wrong one", "wrong two", "wrong three"], "answer_id": 0}
    response = client.post("/api/runs", headers=HEADERS, json={
        "name": "correct expression", "scope": "evaluation", "predictions": [{"paper_id": pid, "questions": [question]}],
    })
    assert response.status_code == 201
    run = response.json()
    report = run["report"]
    assert report["counts"]["matched"] == 1
    assert report["metrics"]["quality_matched_pct"] == 100
    assert report["metrics"]["final_score"] == pytest.approx(100 / report["counts"]["gold"], abs=1e-6)
    paper = next(paper for paper in report["papers"] if paper["paper_id"] == pid)
    assert_hidden(paper, dataset)
    assert paper["missing_count"] == paper["gold_count"] - 1
    assert paper["items"][0]["expression_text"] == expression
    assert paper["items"][0]["matched"] is True
    exported = client.get(f'/api/runs/{run["id"]}/export').json()["report"]
    assert exported == report


def test_partial_matches_never_reveal_hidden_reference_text(client):
    dataset = client.app.state.dataset
    pid, expression = next(
        (pid, item["text"]) for pid in dataset.splits["evaluation"] for item in dataset.labels[pid]
        if len(item["text"].split()) >= 2 and item["text"].split()[0].lower() not in FUNCTION_WORDS
    )
    shorter = expression.split()[0]
    question = {"expression_text": shorter, "options": [shorter, "zz1", "zz2", "zz3"], "answer_id": 0}
    response = client.post("/api/runs", headers=HEADERS, json={
        "name": "partial", "scope": "evaluation", "predictions": [{"paper_id": pid, "questions": [question]}],
    })
    assert response.status_code == 201
    run = response.json()
    paper = next(paper for paper in run["report"]["papers"] if paper["paper_id"] == pid)
    item = paper["items"][0]
    assert item["matched"] is True and 0 < item["match_credit"] < 1
    assert "matched_expression" not in item
    for fetched in (client.get(f'/api/runs/{run["id"]}').json(), client.get(f'/api/runs/{run["id"]}/export').json()):
        paper = next(paper for paper in fetched["report"]["papers"] if paper["paper_id"] == pid)
        assert "matched_expression" not in paper["items"][0]
        assert "gold_expressions" not in paper and "missing_expressions" not in paper


@pytest.mark.parametrize("old_scope", ["all", "train", "evaluation"])
def test_historical_mixed_scope_reads_and_review_responses_hide_only_evaluation_labels(client, old_scope):
    dataset = client.app.state.dataset
    example_id = dataset.splits["demo"][0]
    eval_id = "2208.09462"  # Former training data is private even in old training records.
    expression = dataset.labels[eval_id][0]["text"]
    question = {"expression_text": expression, "options": [expression, "wrong one", "wrong two", "wrong three"], "answer_id": 0}
    report = evaluate(dataset, [{"paper_id": eval_id, "questions": [question]}], [example_id, eval_id], old_scope)
    report["split_version"] = "train-evaluation-v1"
    stored = client.app.state.store.create("legacy mixed", report)
    for suffix in ("", "/export"):
        visible = client.get(f'/api/runs/{stored["id"]}{suffix}').json()["report"]
        assert visible["papers"][0] == report["papers"][0]
        assert_hidden(visible["papers"][1], dataset)
        assert visible["scope"] == report["scope"]
        assert visible["metrics"] == report["metrics"]
        assert visible["split_version"] == "train-evaluation-v1"
    assert client.app.state.store.get(stored["id"]) == stored  # Read projection never rewrites storage.
    skipped = client.post(f'/api/runs/{stored["id"]}/skip-judge', headers=HEADERS).json()
    assert_hidden(skipped["report"]["papers"][1], dataset)
    item = report["papers"][1]["items"][0]
    reviewed = client.post(f'/api/runs/{stored["id"]}/reviews', headers=HEADERS, json={
        "paper_id": eval_id, "question_id": item["question_id"], "accepted_option_ids": [item["answer_id"]],
        "rubric": {"plausibility": 2, "learning_alignment": 2, "distinctness": 2, "no_cues": 2},
        "gates": {"context_sufficient": True, "no_specialist_knowledge": True, "no_answer_leak": True},
        "reviewer": "test", "rationale": "Mock legacy review for response privacy",
    })
    assert reviewed.status_code == 200, reviewed.text
    assert_hidden(reviewed.json()["report"]["papers"][1], dataset)
    assert reviewed.json()["report"]["metrics"]["final_score"] > 0
    assert client.app.state.store.get(stored["id"])["report"]["papers"][1]["gold_expressions"] == report["papers"][1]["gold_expressions"]


@pytest.mark.parametrize("path", ["all/papers", "custom/labels", "evaluation/unknown", "train/../../.env"])
def test_downloads_accept_only_named_public_resources(client, path):
    assert client.get("/api/datasets/" + path).status_code in {404, 422}
