"""Code-only scoring must be immediate, deterministic and explicitly nonsemantic."""
import copy
import csv
import io
from dataclasses import replace
from itertools import permutations

import pytest
from fastapi.testclient import TestClient

from paperlingo.app import create_app
from paperlingo.code_review import CODE_REVIEW_POLICY, code_quality
from paperlingo.config import Settings
from paperlingo.dataset import Dataset
from paperlingo.scoring import evaluate, recompute
from paperlingo.storage import Store

HEADERS = {"X-Requested-With": "PaperLingo"}


@pytest.fixture(scope="module")
def dataset():
    return Dataset(Settings().data_dir)


@pytest.fixture
def settings(tmp_path):
    # Deliberately configured: neither health requests nor inference may run.
    return Settings(runtime_dir=tmp_path, judge_base_url="http://must-not-call.invalid/v1",
                    judge_model="not-used", public_access=True)


@pytest.fixture
def client(settings, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Code mode must not call the LLM or its health endpoint")
    monkeypatch.setattr("paperlingo.app.judge_item", forbidden)
    monkeypatch.setattr("paperlingo.app.httpx.AsyncClient.get", forbidden)
    with TestClient(create_app(settings)) as http:
        yield http


def submit(client, predictions=None, scope="demo"):
    if predictions is None:
        predictions = client.get("/api/sample").json()["predictions"]
    response = client.post("/api/runs", headers=HEADERS, json={
        "name": "코드 검사 회귀 테스트", "scope": scope, "predictions": predictions,
    })
    assert response.status_code == 201, response.text
    return response.json()


def items(run):
    return [item for paper in run["report"]["papers"] for item in paper["items"]]


def scored(dataset, question):
    report = evaluate(dataset, [{"paper_id": "0911.3369", "questions": [question]}],
                      ["0911.3369"], "test")
    report["quality_policy_version"] = CODE_REVIEW_POLICY
    recompute(report)
    return report["papers"][0]["items"][0]


def question():
    return {"expression_text": "identical to", "options": ["to", "with", "of", "for"], "answer_id": 0}


def checks(item):
    return {entry["key"]: entry["passed"] for entry in item["quality"]["checks"]}


def test_default_is_code_and_demo_finishes_without_llm(client):
    config = client.get("/api/config").json()
    assert config["review_mode"] == "code"
    assert config["judge"]["configured"] is True
    assert config["judge"]["automatic"] is False
    assert client.get("/api/judge/status").json()["mode"] == "code"
    run = submit(client)
    assert run["status"] == "complete"
    assert run["judge_meta"] == {}
    report = run["report"]
    assert report["quality_policy_version"] == CODE_REVIEW_POLICY
    assert report["score_mode"] == "code"
    assert report["counts"]["code_passed"] == 9
    assert report["counts"]["code_failed"] == 0
    assert report["metrics"]["final_score"] == 100
    assert report["metrics"]["quality_matched_pct"] == 100
    assert report["metrics"]["unique_answer_rate_pct"] is None
    assert report["metrics"]["source_unique_answer_rate_pct"] == 100
    for item in items(run):
        quality = item["quality"]
        assert quality["result"] == "pass" and quality["source"] == "code"
        assert len(quality["checks"]) == 4 and all(checks(item).values())
        assert quality["semantic_reviewed"] is False
        assert quality["accepted_option_ids"] == quality["option_assessments"] == []
        assert quality["gates"] is None and quality["rubric"] is None


@pytest.mark.parametrize("options", list(permutations(["to", "with", "of", "for"])))
def test_option_order_and_updated_index_do_not_change_score(dataset, options):
    q = question()
    q.update(options=list(options), answer_id=options.index("to"))
    item = scored(dataset, q)
    assert item["quality"]["result"] == "pass"
    assert item["quality"]["source_match_option_ids"] == ["ABCD"[options.index("to")]]


@pytest.mark.parametrize("options", [[], ["to"], ["to", "with", "of"],
    ["to", "with", "of", "for", "by"], ["to", "", "of", "for"],
    ["to", "  ", "of", "for"], ["to", None, "of", "for"],
    ["to", {"text": "with"}, "of", "for"], ["to", "x" * 1001, "of", "for"], None])
def test_invalid_option_format_is_retained_as_fail(dataset, options):
    q = question(); q["options"] = options
    item = scored(dataset, q)
    assert checks(item)["option_format"] is False
    assert item["quality"]["q"] == 0
    assert item["quality"]["result"] == "fail"


@pytest.mark.parametrize("duplicate", ["to", "TO", " to ", "ｔｏ", "\tTo\n"])
def test_normalized_answer_duplicates_fail_both_checks(dataset, duplicate):
    q = question(); q["options"][1] = duplicate
    item = scored(dataset, q)
    assert checks(item) == {"option_format": True, "no_duplicates": False,
                            "designated_answer": True, "unique_source_answer": False}
    assert item["quality"]["q"] == 0


def test_duplicated_distractors_fail_even_with_unique_source_answer(dataset):
    q = question(); q["options"][2] = "WITH"
    item = scored(dataset, q)
    assert checks(item)["unique_source_answer"] is True
    assert checks(item)["no_duplicates"] is False
    assert item["quality"]["result"] == "fail"


@pytest.mark.parametrize("index", [-1, 4, None, True, False, "0", "A", 0.0, [], {}])
def test_invalid_designated_index_is_not_coerced(dataset, index):
    q = question(); q["answer_id"] = index
    item = scored(dataset, q)
    assert checks(item)["designated_answer"] is False
    assert item["quality"]["result"] == "fail"


def test_wrong_designated_option_cannot_restore_expression(dataset):
    q = question(); q["answer_id"] = 1
    item = scored(dataset, q)
    assert checks(item)["designated_answer"] is False
    assert item["quality"]["result"] == "fail"


def test_source_checks_do_not_claim_semantic_uniqueness_or_context_review(dataset):
    q = {"expression_text": "identical to", "options": ["identical to", "the same as", "different from", "similar to"], "answer_id": 0}
    item = scored(dataset, q)
    # "the same as" could be semantically acceptable, but is not source-identical.
    assert item["quality"]["result"] == "pass"
    assert item["quality"]["semantic_reviewed"] is False
    assert item["quality"]["gates"] is None
    assert "의미적" in item["quality"]["rationale"]


def test_legacy_blank_source_is_independent_of_answer_key(dataset):
    q = copy.deepcopy(dataset.demo_predictions()[0]["questions"][0])
    correct = q["answer_id"]
    q["answer_id"] = next(option["id"] for option in q["options"] if option["id"] != correct)
    item = scored(dataset, q)
    assert checks(item)["designated_answer"] is False
    assert checks(item)["unique_source_answer"] is True
    assert item["quality"]["source_match_option_ids"] == [correct]


def test_input_integrity_failures_cannot_get_credit_even_if_four_checks_pass(dataset):
    q = copy.deepcopy(dataset.demo_predictions()[0]["questions"][0])
    q["expression_span"]["start"] += 1
    item = scored(dataset, q)
    assert all(checks(item).values())
    assert item["quality"]["result"] == "fail"
    assert item["issues"] and item["quality"]["rationale"]


def test_recall_and_binary_pass_rate_are_multiplied(client):
    predictions = client.get("/api/sample").json()["predictions"]
    predictions[0]["questions"] = predictions[0]["questions"][:2]
    predictions[1]["questions"] = []
    bad = predictions[0]["questions"][0]
    bad["options"][(bad["answer_id"] + 1) % 4] = bad["options"][bad["answer_id"]]
    run = submit(client, predictions)
    assert [item["quality"]["q"] for item in items(run)] == [0, 1]
    assert run["report"]["counts"]["matched"] == 2
    assert run["report"]["metrics"]["recall_pct"] == pytest.approx(22.222222, abs=0.0001)
    assert run["report"]["metrics"]["quality_matched_pct"] == 50
    assert run["report"]["metrics"]["final_score"] == pytest.approx(11.111111, abs=0.0001)


def test_code_results_cannot_be_overwritten_and_exports_include_checks(client):
    run = submit(client)
    for action in ("judge", "skip-judge", "reviews"):
        response = client.post(f'/api/runs/{run["id"]}/{action}', headers=HEADERS, json={})
        assert response.status_code == 409
    export = client.get(f'/api/runs/{run["id"]}/export').json()
    assert export["report"] == run["report"]
    assert export["review_history"] == []
    csv_text = client.get(f'/api/runs/{run["id"]}/export?format=csv').text.lstrip("\ufeff")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == 10  # Nine items plus the explicit empty-gold submission.
    assert all(row["check_result"] == "pass" and row["designated_answer"] == "True"
               for row in rows if row["question_id"])
    assert all(None not in row for row in rows)


def test_example_download_scores_full_marks_without_inference(client):
    data = client.get("/api/datasets/demo/answers").text
    run = submit(client, data, "demo")
    assert run["status"] == "complete"
    assert run["report"]["counts"]["predicted"] == 9
    assert run["report"]["counts"]["code_passed"] == 9
    assert run["report"]["metrics"]["recall_pct"] == 100
    assert run["report"]["metrics"]["quality_matched_pct"] == 100
    assert run["report"]["metrics"]["final_score"] == 100


def test_code_startup_preserves_legacy_completed_and_pending_scores(settings, dataset, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Code mode must not resume legacy LLM jobs")
    monkeypatch.setattr("paperlingo.app.judge_item", forbidden)
    store = Store(settings.runtime_dir)
    report = evaluate(dataset, dataset.demo_predictions(compact=True), dataset.splits["demo"], "demo")
    pending = store.create("legacy pending", report)
    queued = store.create("legacy queued", report)
    queued.update(status="queued", judge_meta={"job_id": "interrupted"})
    store.save(queued)
    from paperlingo.judge import temporary_quality
    for paper in report["papers"]:
        for item in paper["items"]:
            item["quality"] = temporary_quality(item)
    recompute(report)
    completed = store.create("legacy complete", report)
    with TestClient(create_app(settings)) as client:
        assert client.get(f'/api/runs/{pending["id"]}').json() == pending
        assert client.get(f'/api/runs/{completed["id"]}').json() == completed
        recovered = client.get(f'/api/runs/{queued["id"]}').json()
        assert recovered["status"] == "awaiting_review"
        assert recovered["report"] == queued["report"]
        assert client.get("/api/config").json()["review_mode"] == "code"


def test_code_records_are_protected_after_switching_to_llm(settings, client):
    run = submit(client)
    with TestClient(create_app(replace(settings, review_mode="llm"))) as legacy:
        assert legacy.post(f'/api/runs/{run["id"]}/judge', headers=HEADERS).status_code == 409
        assert legacy.get(f'/api/runs/{run["id"]}').json() == run


def test_invalid_review_mode_rejected(settings):
    with pytest.raises(ValueError, match="PAPERLINGO_REVIEW_MODE"):
        replace(settings, review_mode="invalid").validate()
