"""Public API regressions for recall scoring and immutable historical formulas."""

from copy import deepcopy
import csv
import io
import json
import time

from fastapi.testclient import TestClient
import pytest

from paperlingo.app import create_app as create_web_app
from paperlingo.config import Settings
from paperlingo.dataset import Dataset
from paperlingo.judge import quality_from_review
from paperlingo.scoring import evaluate, recompute
from paperlingo.storage import Store


HEADERS = {"X-Requested-With": "PaperLingo"}
NEW_VERSION = "exact-rq-v3"
OLD_VERSIONS = ("exact-prq-v1", "exact-prq-v2", None)
# Only the first submitted expression is gold; the other expressions still occur
# verbatim in the abstract and produce structurally valid questions.
SUBMITTED = ["consistent with", "similar to", "a function of", "associated with", "based on", "depend on"]


def create_app(settings):
    """Keep denominator fixtures fixed while public scopes use the new names.

    Production partitioning is tested separately. Each scope here has one
    paper so review assertions stay focused on the submitted items.
    """
    app = create_web_app(settings)
    app.state.dataset.splits = {"demo": ["p1"], "evaluation": ["p2"]}
    return app


@pytest.fixture
def settings(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    papers = [
        {"paper_id": "p1", "paper_name": "Two reference expressions", "domain": {"code": "test"},
         "abstract": "Results are consistent with evidence. Results depend on controls. The outcome is similar to expectations. The result is a function of time. Changes are associated with pressure. This is based on observations."},
        {"paper_id": "p2", "paper_name": "One reference expression", "domain": {"code": "test"},
         "abstract": "Our conclusions depend on observations."},
    ]
    labels = [
        {"paper_id": "p1", "expressions": [{"text": "consistent with"}, {"text": "depend on"}]},
        {"paper_id": "p2", "expressions": [{"text": "depend on"}]},
    ]
    for filename, rows in (("abstract_data.jsonl", papers), ("abstract_labels_v2.jsonl", labels)):
        (data_dir / filename).write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return Settings(review_mode="llm", data_dir=data_dir, runtime_dir=tmp_path / "runtime", judge_concurrency=1)


def predictions(count=3):
    questions = []
    for expression in SUBMITTED[:count]:
        answer = expression.split()[-1]
        options = [answer, *[value for value in ("at", "for", "of", "to", "on") if value != answer][:3]]
        questions.append({"expression_text": expression, "options": options, "answer_id": 0})
    return [{"paper_id": "p1", "questions": questions}]


def items(run):
    return [item for paper in run["report"]["papers"] for item in paper["items"]]


def submit(client, *, count=3, scope="demo"):
    return client.post("/api/runs", headers=HEADERS, json={
        "name": "회수율 지표 회귀 검사", "scope": scope,
        "predictions": predictions(count),
    })


def reviewed_quality(item, *, source="human", points=2):
    rubric = {"plausibility": points, "learning_alignment": points, "distinctness": points, "no_cues": points}
    common = {
        "rubric": rubric,
        "gates": {"context_sufficient": True, "no_specialist_knowledge": True, "no_answer_leak": True},
        "rationale": "점수 계산 확인용 모의 검수",
    }
    if source == "human":
        payload = {**common, "accepted_option_ids": [item["answer_id"]], "reviewer": "테스트 검수자"}
    else:
        payload = {**common, "option_assessments": [
            {"id": option["id"], "verdict": "acceptable" if option["id"] == item["answer_id"] else "incorrect",
             "reason": "모의 보기 검수"} for option in item["options"]
        ]}
    return quality_from_review(item, payload, source=source)


def seed_legacy(settings, version, *, queued=False):
    dataset = Dataset(settings.data_dir)
    report = evaluate(dataset, predictions(), ["p1"], "custom")
    if version is None:
        report.pop("metrics_version")
    else:
        report["metrics_version"] = version
    if not queued:
        for paper in report["papers"]:
            for item in paper["items"]:
                item["quality"] = reviewed_quality(item, points=1 if item["matched"] else 2)
    recompute(report)
    store = Store(settings.runtime_dir)
    run = store.create("이전 버전 보존 검사", report)
    if queued:
        run["status"] = "queued"
        run["judge_meta"] = {"job_id": "old-interrupted-job", "total": 3, "processed": 0}
        store.save(run)
    return run


def wait_done(client, run_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] not in {"queued", "judging"}:
            return run
        time.sleep(0.005)
    pytest.fail("Mock judge did not finish")


def configured(settings):
    from dataclasses import replace
    return replace(settings, judge_base_url="http://never-called.invalid/v1", judge_model="mock-only")


def assert_old_formula(run, version, quality):
    report = run["report"]
    assert report["scope"] == {"name": "custom", "paper_ids": ["p1"], "paper_count": 1}
    assert report.get("metrics_version") == version
    if version is None:
        assert "metrics_version" not in report
    assert report["counts"]["gold"] == 2
    assert report["counts"]["predicted"] == 3
    assert report["counts"]["matched"] == 1
    assert report["metrics"]["precision_pct"] == pytest.approx(100 / 3)
    assert report["metrics"]["recall_pct"] == 50
    assert report["metrics"]["quality_matched_pct"] == quality * 100
    assert report["metrics"]["final_score"] == pytest.approx(100 / 3 * 0.5 * quality)


def test_new_compact_five_questions_use_recall_not_precision(settings):
    with TestClient(create_app(settings)) as client:
        response = submit(client, count=5)
        assert response.status_code == 201, response.text
        run = response.json()
        assert run["report"]["scope"] == {"name": "demo", "paper_ids": ["p1"], "paper_count": 1}
        assert run["report"]["metrics_version"] == NEW_VERSION
        assert all(item["structure_valid"] for item in items(run))
        skipped = client.post(f'/api/runs/{run["id"]}/skip-judge', headers=HEADERS)
        assert skipped.status_code == 200
        report = skipped.json()["report"]
        assert report["counts"]["gold"] == 2
        assert report["counts"]["predicted"] == 5
        assert report["counts"]["matched"] == 1
        assert report["metrics"]["precision_pct"] == 20  # Still shown as a diagnostic.
        assert report["metrics"]["recall_pct"] == 50
        assert report["metrics"]["quality_matched_pct"] == 100
        assert report["metrics"]["final_score"] == 50  # Old P×R×Q would have been 10.
        exported = client.get(f'/api/runs/{run["id"]}/export').json()
        assert exported["report"]["metrics_version"] == NEW_VERSION
        assert len(exported["review_history"]) == 5
        # Q still matters, and only matched questions contribute to final Q.
        # Lower the matched question to 50% and an unmatched question to zero.
        for position, item in enumerate(items(exported)[:2]):
            response = client.post(f'/api/runs/{run["id"]}/reviews', headers=HEADERS, json={
                "paper_id": "p1", "question_id": item["question_id"],
                "accepted_option_ids": [item["answer_id"]] if position == 0 else [],
                "rubric": {"plausibility": 1, "learning_alignment": 1, "distinctness": 1, "no_cues": 1},
                "gates": {"context_sufficient": True, "no_specialist_knowledge": True, "no_answer_leak": True},
                "rationale": "일치 문항 품질과 전체 품질 구분 검증", "reviewer": "테스트 검수자",
            })
            assert response.status_code == 200
        corrected = response.json()["report"]
        assert corrected["metrics_version"] == NEW_VERSION
        assert corrected["metrics"]["quality_all_pct"] == 70
        assert corrected["metrics"]["quality_matched_pct"] == 50
        assert corrected["metrics"]["final_score"] == 25


def test_recall_denominator_uses_actual_labels_including_missing_papers(settings):
    app = create_app(settings)
    app.state.dataset.splits = {"demo": ["p1", "p2"], "evaluation": []}
    with TestClient(app) as client:
        response = submit(client, count=3, scope="demo")
        assert response.status_code == 201
        run = response.json()
        skipped = client.post(f'/api/runs/{run["id"]}/skip-judge', headers=HEADERS).json()
        report = skipped["report"]
        assert report["scope"] == {"name": "demo", "paper_ids": ["p1", "p2"], "paper_count": 2}
        assert report["counts"]["gold"] == 3  # Two labels plus one, not five per paper.
        assert report["counts"]["missing_papers"] == 1
        assert report["metrics"]["recall_pct"] == pytest.approx(100 / 3)
        assert report["metrics"]["final_score"] == pytest.approx(100 / 3)


def test_six_questions_are_rejected_instead_of_truncated(settings):
    with TestClient(create_app(settings)) as client:
        response = submit(client, count=6)
        assert response.status_code == 422
        assert "최대 5개" in response.text
        assert client.get("/api/runs").json()["items"] == []


@pytest.mark.parametrize("version", OLD_VERSIONS)
def test_old_formula_survives_human_review_skip_model_retry_and_export(settings, monkeypatch, version):
    run = seed_legacy(settings, version)
    assert_old_formula(run, version, 0.5)
    calls = []

    async def mock_judge(item, config):
        calls.append(item["question_id"])
        return reviewed_quality(item, source="llm", points=1 if item["matched"] else 2)

    monkeypatch.setattr("paperlingo.app.judge_item", mock_judge)
    with TestClient(create_app(configured(settings))) as client:
        matched = next(item for item in items(run) if item["matched"])
        payload = {
            "paper_id": "p1", "question_id": matched["question_id"],
            "accepted_option_ids": [matched["answer_id"]],
            "rubric": {"plausibility": 2, "learning_alignment": 2, "distinctness": 1, "no_cues": 1},
            "gates": {"context_sufficient": True, "no_specialist_knowledge": True, "no_answer_leak": True},
            "rationale": "구버전 계산 보존 확인", "reviewer": "테스트 검수자",
        }
        reviewed = client.post(f'/api/runs/{run["id"]}/reviews', headers=HEADERS, json=payload)
        assert reviewed.status_code == 200
        assert_old_formula(reviewed.json(), version, 0.75)
        skipped = client.post(f'/api/runs/{run["id"]}/skip-judge', headers=HEADERS)
        assert skipped.status_code == 200
        assert_old_formula(skipped.json(), version, 1)
        before_retry = client.get(f'/api/runs/{run["id"]}/export').json()
        assert len(before_retry["review_history"]) == 4
        assert before_retry["review_history"][0]["previous_quality"]["q"] == 0.5
        assert before_retry["review_history"][0]["quality"]["q"] == 0.75
        assert client.post(f'/api/runs/{run["id"]}/judge', headers=HEADERS).status_code == 200
        finished = wait_done(client, run["id"])
        assert finished["status"] == "complete"
        assert_old_formula(finished, version, 0.5)
        assert len(calls) == 3
        exported = client.get(f'/api/runs/{run["id"]}/export').json()
        assert_old_formula(exported, version, 0.5)
        assert exported["review_history"][:4] == before_retry["review_history"]
        assert len(exported["review_history"]) == 7
        assert all(entry["previous_quality"]["source"] == "temporary" for entry in exported["review_history"][4:])
        summary = client.get("/api/runs").json()["items"][0]
        assert summary["metrics_version"] == version
        assert summary["metrics"]["final_score"] == pytest.approx(100 / 12)
        downloaded = client.get(f'/api/runs/{run["id"]}/export?format=csv')
        assert downloaded.status_code == 200
        rows = list(csv.DictReader(io.StringIO(downloaded.text.lstrip("\ufeff"))))
        assert len(rows) == 3
        assert all(row["metrics_version"] == (version or "") for row in rows)
    # A completed record must survive a new application lifespan without being
    # regraded, relabeled with the latest version, or altering its audit history.
    with TestClient(create_app(configured(settings))) as client:
        restarted = client.get(f'/api/runs/{run["id"]}/export').json()
        assert_old_formula(restarted, version, 0.5)
        assert restarted["review_history"] == exported["review_history"]
        assert len(calls) == 3


@pytest.mark.parametrize("version", OLD_VERSIONS)
def test_startup_resume_keeps_queued_legacy_formula(settings, monkeypatch, version):
    run = seed_legacy(settings, version, queued=True)
    original_report = deepcopy(run["report"])
    calls = []

    async def mock_judge(item, config):
        calls.append(item["question_id"])
        return reviewed_quality(item, source="llm", points=1 if item["matched"] else 2)

    monkeypatch.setattr("paperlingo.app.judge_item", mock_judge)
    with TestClient(create_app(configured(settings))) as client:
        finished = wait_done(client, run["id"])
        assert finished["status"] == "complete"
        assert_old_formula(finished, version, 0.5)
        assert finished["judge_meta"]["automatic"] is True
        assert len(calls) == 3
        assert finished["report"]["dataset_version"] == original_report["dataset_version"]
        exported = client.get(f'/api/runs/{run["id"]}/export').json()
        assert exported["report"].get("metrics_version") == version
        assert len(exported["review_history"]) == 3
        assert all(entry["previous_quality"]["status"] == "pending" for entry in exported["review_history"])
