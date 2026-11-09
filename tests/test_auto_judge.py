"""Automation and cancellation integration tests; all model calls are mocked."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import threading
import time

from fastapi.testclient import TestClient
import pytest

from paperlingo.app import create_app as create_web_app
from paperlingo.config import Settings
from paperlingo.judge import quality_from_review
from paperlingo.scoring import recompute
from paperlingo.storage import Store


HEADERS = {"X-Requested-With": "PaperLingo"}
EXPRESSIONS = ["consistent with", "depend on", "a function of", "similar to"]


def create_app(settings):
    """Pin small semantic scopes independently of the production partition.

    These tests exercise queueing and review lifecycles. Dataset partitions
    have separate tests; deterministic one-paper scopes keep these assertions
    about the same submitted items after the public scope simplification.
    """
    app = create_web_app(settings)
    app.state.dataset.splits = {"demo": ["p1"], "evaluation": ["p2"]}
    return app


@pytest.fixture
def settings(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    papers = [
        {"paper_id": "p1", "paper_name": "First", "domain": {"code": "test"},
         "abstract": "Results are consistent with evidence. Results depend on controls. The result is a function of time. Results are similar to expectations."},
        {"paper_id": "p2", "paper_name": "Second", "domain": {"code": "test"},
         "abstract": "Our findings are consistent with the hypothesis."},
    ]
    labels = [{"paper_id": "p1", "expressions": [{"text": value} for value in EXPRESSIONS]},
              {"paper_id": "p2", "expressions": [{"text": EXPRESSIONS[0]}]}]
    for filename, rows in (("abstract_data.jsonl", papers), ("abstract_labels_v2.jsonl", labels)):
        (data / filename).write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return Settings(review_mode="llm", data_dir=data, runtime_dir=tmp_path / "runtime",
                    judge_base_url="http://never-called.invalid/v1", judge_model="mock-only",
                    judge_concurrency=1, judge_batch_size=2)


def predictions(count=3, paper_id="p1", invalid=False):
    questions = []
    for expression in EXPRESSIONS[:count]:
        answer = expression.split()[-1]
        options = [answer, *[value for value in ("at", "for", "of", "to", "on") if value != answer][:3]]
        questions.append({"expression_text": expression, "options": options, "answer_id": 0})
    if invalid:
        expression = EXPRESSIONS[count]
        answer = expression.split()[-1]
        questions.append({"expression_text": expression, "options": [answer, answer, "other", "another"], "answer_id": 0})
    return [{"paper_id": paper_id, "questions": questions}]


def upload(client, count=3, paper_id="p1", invalid=False):
    scope = {"p1": "demo", "p2": "evaluation"}[paper_id]
    response = client.post("/api/runs", headers=HEADERS, json={
        "name": "자동화 모의 검증", "scope": scope,
        "predictions": predictions(count, paper_id, invalid),
    })
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["report"]["scope"] == {"name": scope, "paper_ids": [paper_id], "paper_count": 1}
    return run


def all_items(run):
    return [item for paper in run["report"]["papers"] for item in paper["items"]]


def llm_quality(item, *, uncertain=False):
    payload = {
        "option_assessments": [{
            "id": option["id"],
            "verdict": "acceptable" if option["id"] == item["answer_id"] else "incorrect",
            "reason": "모의 문맥 판정",
        } for option in item["options"]],
        "rubric": {"plausibility": 2, "learning_alignment": 2, "distinctness": 2, "no_cues": 2},
        "gates": {"context_sufficient": True, "no_specialist_knowledge": True, "no_answer_leak": True},
        "rationale": "외부 호출 없는 모의 판정",
    }
    if uncertain:
        next(value for value in payload["option_assessments"] if value["id"] != item["answer_id"])["verdict"] = "uncertain"
    return quality_from_review(item, payload, source="llm")


def human_payload(run, item, *, accepted=None):
    paper = next(paper for paper in run["report"]["papers"] if item in paper["items"])
    return {
        "paper_id": paper["paper_id"], "question_id": item["question_id"],
        "accepted_option_ids": [item["answer_id"]] if accepted is None else accepted,
        "rubric": {"plausibility": 2, "learning_alignment": 2, "distinctness": 2, "no_cues": 2},
        "gates": {"context_sufficient": True, "no_specialist_knowledge": True, "no_answer_leak": True},
        "rationale": "테스트용 수동 판정", "reviewer": "모의 검수자",
    }


def wait_until(predicate, *, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    pytest.fail("Timed out waiting for the mocked background job")


def wait_done(client, run_id):
    def finished():
        run = client.get(f"/api/runs/{run_id}").json()
        return run if run["status"] not in {"queued", "judging"} else None
    return wait_until(finished)


def wait_idle(client, app, run_id):
    wait_until(lambda: client.portal.call(lambda: run_id not in app.state.jobs))


class ControlledJudge:
    """Hold each mocked call on an event, independently of scheduler timing."""

    def __init__(self):
        self.condition = threading.Condition()
        self.calls = []
        self.events = []
        self.active = 0
        self.max_active = 0

    async def __call__(self, item, config):
        event = asyncio.Event()
        with self.condition:
            self.calls.append(deepcopy(item))
            self.events.append(event)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.condition.notify_all()
        try:
            await event.wait()
            return llm_quality(item)
        finally:
            with self.condition:
                self.active -= 1

    def wait_calls(self, count):
        with self.condition:
            assert self.condition.wait_for(lambda: len(self.calls) >= count, timeout=3), "Mock call did not start"

    def release(self, client, number):
        client.portal.call(self.events[number - 1].set)


def test_upload_automatically_serializes_runs_and_processes_every_chunk(settings, monkeypatch):
    controlled = ControlledJudge()
    monkeypatch.setattr("paperlingo.app.judge_item", controlled)
    app = create_app(replace(settings, judge_batch_size=1, judge_concurrency=2))
    with TestClient(app) as client:
        first = upload(client, 3)
        controlled.wait_calls(1)
        second = upload(client, 1, "p2")
        assert second["status"] == "queued"
        duplicate = client.post(f'/api/runs/{second["id"]}/judge', headers=HEADERS)
        assert duplicate.status_code == 200
        assert duplicate.json()["status"] == "queued"
        assert client.get(f'/api/runs/{second["id"]}').json()["judge_meta"]["job_id"] == second["judge_meta"]["job_id"]
        for number in range(1, 4):
            controlled.wait_calls(number)
            assert len(controlled.calls) == number
            assert client.get(f'/api/runs/{second["id"]}').json()["status"] == "queued"
            controlled.release(client, number)
        controlled.wait_calls(4)
        completed_first = wait_done(client, first["id"])
        assert completed_first["judge_meta"]["processed"] == 3
        assert completed_first["judge_meta"]["total"] == 3
        assert completed_first["report"]["metrics"]["quality_all_pct"] == 100
        controlled.release(client, 4)
        assert wait_done(client, second["id"])["status"] == "complete"
        assert [item["expression_text"] for item in controlled.calls] == [*EXPRESSIONS[:3], EXPRESSIONS[0]]
        assert controlled.max_active == 1


def test_skip_inflight_preserves_manual_overwrite_and_discards_late_result(settings, monkeypatch):
    controlled = ControlledJudge()
    monkeypatch.setattr("paperlingo.app.judge_item", controlled)
    app = create_app(replace(settings, judge_batch_size=100))
    with TestClient(app) as client:
        run = upload(client, 2, invalid=True)
        controlled.wait_calls(1)
        response = client.post(f'/api/runs/{run["id"]}/skip-judge', headers=HEADERS)
        assert response.status_code == 200
        skipped = response.json()
        assert skipped["status"] == "complete"
        assert skipped["judge_meta"]["job_id"] is None
        assert [item["quality"]["q"] for item in all_items(skipped)] == [1, 1, 0]
        assert [item["quality"]["source"] for item in all_items(skipped)] == ["temporary", "temporary", "structural"]
        item = all_items(skipped)[0]
        review = client.post(f'/api/runs/{run["id"]}/reviews', headers=HEADERS,
                             json=human_payload(skipped, item, accepted=[]))
        assert review.status_code == 200, review.text
        before_drain = client.get(f'/api/runs/{run["id"]}/export').json()
        assert len(before_drain["review_history"]) == 3
        controlled.release(client, 1)
        wait_idle(client, app, run["id"])
        after_drain = client.get(f'/api/runs/{run["id"]}/export').json()
        assert after_drain["review_history"] == before_drain["review_history"]
        assert after_drain["report"] == before_drain["report"]
        assert all_items(after_drain)[0]["quality"]["source"] == "human"
        assert all_items(after_drain)[0]["quality"]["q"] == 0
        assert len(controlled.calls) == 1  # Waiting items never invoke the model.


def test_immediate_requeue_after_skip_survives_old_task_cleanup(settings, monkeypatch):
    controlled = ControlledJudge()
    monkeypatch.setattr("paperlingo.app.judge_item", controlled)
    app = create_app(settings)
    with TestClient(app) as client:
        run = upload(client, 2)
        controlled.wait_calls(1)
        assert client.post(f'/api/runs/{run["id"]}/skip-judge', headers=HEADERS).status_code == 200
        assert client.post(f'/api/runs/{run["id"]}/judge', headers=HEADERS).status_code == 200
        requeued = client.get(f'/api/runs/{run["id"]}').json()
        assert requeued["status"] == "queued"
        assert requeued["judge_meta"]["job_id"] != run["judge_meta"]["job_id"]
        assert len(controlled.calls) == 1  # New work waits for the old GPU call to drain.
        controlled.release(client, 1)
        controlled.wait_calls(2)
        assert client.portal.call(lambda: run["id"] in app.state.jobs)
        assert client.get(f'/api/runs/{run["id"]}').json()["judge_meta"]["job_id"] == requeued["judge_meta"]["job_id"]
        controlled.release(client, 2)
        controlled.wait_calls(3)
        controlled.release(client, 3)
        finished = wait_done(client, run["id"])
        assert finished["judge_meta"]["processed"] == 2
        assert all(item["quality"]["source"] == "llm" for item in all_items(finished))
        history = client.get(f'/api/runs/{run["id"]}/export').json()["review_history"]
        assert [entry["quality"]["source"] for entry in history] == ["temporary", "temporary", "llm", "llm"]
        assert controlled.max_active == 1


def test_skip_without_model_overwrites_old_reviews_but_not_structure(settings, monkeypatch):
    async def forbidden_call(item, config):
        pytest.fail("No configured judge may be called")
    monkeypatch.setattr("paperlingo.app.judge_item", forbidden_call)
    with TestClient(create_app(replace(settings, judge_base_url="", judge_model=""))) as client:
        run = upload(client, 2, invalid=True)
        for item in all_items(run)[:2]:
            response = client.post(f'/api/runs/{run["id"]}/reviews', headers=HEADERS,
                                   json=human_payload(run, item, accepted=[]))
            assert response.status_code == 200
        assert client.post(f'/api/runs/{run["id"]}/judge', headers=HEADERS).status_code == 409
        skipped = client.post(f'/api/runs/{run["id"]}/skip-judge', headers=HEADERS).json()
        assert skipped["status"] == "complete"
        assert [item["quality"]["q"] for item in all_items(skipped)] == [1, 1, 0]
        assert skipped["report"]["metrics"]["quality_all_pct"] == pytest.approx(200 / 3)
        export = client.get(f'/api/runs/{run["id"]}/export').json()
        assert len(export["review_history"]) == 4
        assert all(entry["previous_quality"]["source"] == "human" for entry in export["review_history"][2:])
        assert all(entry["quality"]["option_assessments"] == [] for entry in export["review_history"][2:])
        assert client.post(f'/api/runs/{run["id"]}/skip-judge', headers=HEADERS).status_code == 200
        assert client.get(f'/api/runs/{run["id"]}/export').json()["review_history"] == export["review_history"]


def test_skip_score_and_audit_history_rollback_together_on_database_failure(settings):
    app = create_app(replace(settings, judge_base_url="", judge_model=""))
    with TestClient(app, raise_server_exceptions=False) as client:
        run = upload(client, 2)
        # Fail the second history row after the run update and first history row
        # were attempted. Neither the temporary scores nor half an audit may persist.
        with app.state.store.connect() as db:
            db.executescript("""
                CREATE TRIGGER fail_second_test_review BEFORE INSERT ON reviews
                WHEN (SELECT COUNT(*) FROM reviews) = 1
                BEGIN SELECT RAISE(ABORT, 'test audit insert failure'); END;
            """)
        response = client.post(f'/api/runs/{run["id"]}/skip-judge', headers=HEADERS)
        assert response.status_code == 500
        stored = app.state.store.get(run["id"])
        assert stored["status"] == run["status"] == "awaiting_review"
        assert stored["report"] == run["report"]
        assert stored["judge_meta"] == run["judge_meta"]
        assert app.state.store.review_history(run["id"]) == []


def test_uncertainty_completes_automatically_without_human_review(settings, monkeypatch):
    async def uncertain_judge(item, config):
        return llm_quality(item, uncertain=True)
    monkeypatch.setattr("paperlingo.app.judge_item", uncertain_judge)
    with TestClient(create_app(settings)) as client:
        run = upload(client, 1)
        finished = wait_done(client, run["id"])
        assert finished["status"] == "complete"
        assert finished["report"]["metrics"]["final_score"] == 0
        quality = all_items(finished)[0]["quality"]
        assert quality["q"] == 0
        assert quality["review_recommended"] is True
        assert quality["source"] == "llm"
        assert any(value["verdict"] == "uncertain" for value in quality["option_assessments"])
        assert client.post(f'/api/runs/{run["id"]}/judge', headers=HEADERS).status_code == 409


def test_provider_error_stops_then_explicit_retry_preserves_resolved_reviews(settings, monkeypatch):
    calls = []
    fail = True

    async def judge(item, config):
        calls.append(item["expression_text"])
        if fail and item["expression_text"] == EXPRESSIONS[1]:
            return {"status": "error", "source": "llm", "q": None,
                    "accepted_option_ids": [], "option_assessments": [], "rubric": None, "gates": None,
                    "rationale": "모의 연결 오류", "reviewer": None, "error_code": "provider_error"}
        return llm_quality(item)

    monkeypatch.setattr("paperlingo.app.judge_item", judge)
    app = create_app(settings)
    with TestClient(app) as client:
        run = upload(client, 3)
        failed = wait_done(client, run["id"])
        wait_idle(client, app, run["id"])
        assert failed["status"] == "error"
        assert failed["report"]["metrics"]["final_score"] is None
        assert calls == EXPRESSIONS[:3]
        for _ in range(3):
            assert client.get(f'/api/runs/{run["id"]}').json()["status"] == "error"
        assert calls == EXPRESSIONS[:3]
        item = all_items(failed)[2]
        assert client.post(f'/api/runs/{run["id"]}/reviews', headers=HEADERS,
                           json=human_payload(failed, item)).status_code == 200
        fail = False
        assert client.post(f'/api/runs/{run["id"]}/judge', headers=HEADERS).status_code == 200
        finished = wait_done(client, run["id"])
        assert finished["status"] == "complete"
        assert calls == [*EXPRESSIONS[:3], EXPRESSIONS[1]]
        assert [item["quality"]["source"] for item in all_items(finished)] == ["llm", "llm", "human"]
        assert finished["judge_meta"]["total"] == 1


def test_shutdown_persists_queue_and_restart_resumes_only_unfinished_items(settings, monkeypatch):
    controlled = ControlledJudge()
    monkeypatch.setattr("paperlingo.app.judge_item", controlled)
    with TestClient(create_app(replace(settings, judge_batch_size=1))) as client:
        run = upload(client, 3)
        controlled.wait_calls(1)
        controlled.release(client, 1)
        controlled.wait_calls(2)
        assert all_items(client.get(f'/api/runs/{run["id"]}').json())[0]["quality"]["source"] == "llm"
        # Lifespan cancellation interrupts this blocked second call.
    stored = Store(settings.runtime_dir).get(run["id"])
    assert stored["status"] == "queued"
    assert stored["judge_meta"]["interrupted"] is True
    calls = []

    async def resumed_judge(item, config):
        calls.append(item["expression_text"])
        return llm_quality(item)

    monkeypatch.setattr("paperlingo.app.judge_item", resumed_judge)
    with TestClient(create_app(settings)) as client:
        finished = wait_done(client, run["id"])
        assert finished["status"] == "complete"
        assert calls == EXPRESSIONS[1:3]
        history = client.get(f'/api/runs/{run["id"]}/export').json()["review_history"]
        assert len(history) == 3
        assert len({entry["question_id"] for entry in history}) == 3


@pytest.mark.parametrize("case", ["already_complete", "no_configuration", "dataset_mismatch"])
def test_recovery_does_not_leave_orphaned_queued_runs(settings, monkeypatch, case):
    calls = []

    async def unexpected_judge(item, config):
        calls.append(item["question_id"])
        return llm_quality(item)

    monkeypatch.setattr("paperlingo.app.judge_item", unexpected_judge)
    unconfigured = replace(settings, judge_base_url="", judge_model="")
    app = create_app(unconfigured)
    with TestClient(app) as client:
        run = upload(client, 1)
        if case == "already_complete":
            item = all_items(run)[0]
            item["quality"] = llm_quality(item)
            recompute(run["report"])
        if case == "dataset_mismatch":
            run["report"]["dataset_version"] = "different-dataset-version"
        run["status"] = "judging"
        run["judge_meta"] = {"job_id": "interrupted-job", "total": 1, "processed": int(case == "already_complete")}
        app.state.store.save(run)
    restart_settings = unconfigured if case == "no_configuration" else settings
    with TestClient(create_app(restart_settings)) as client:
        restored = client.get(f'/api/runs/{run["id"]}').json()
        assert restored["status"] == {
            "already_complete": "complete", "no_configuration": "awaiting_review", "dataset_mismatch": "error",
        }[case]
        assert calls == []
        if case == "dataset_mismatch":
            assert client.post(f'/api/runs/{run["id"]}/judge', headers=HEADERS).status_code == 409
