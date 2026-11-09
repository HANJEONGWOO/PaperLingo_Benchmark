"""Public API adversarial cases that must not poison stored evaluations."""

import asyncio
import json
import threading
import time

from fastapi.testclient import TestClient
import pytest

from paperlingo.app import create_app
from paperlingo.config import Settings
from paperlingo.judge import quality_from_review


HEADERS = {"X-Requested-With": "PaperLingo", "Content-Type": "application/json"}


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app, raise_server_exceptions=False) as http:
        yield http, app


def test_paper_ids_containing_slash_are_accessible(client):
    http, _ = client
    response = http.get("/api/papers/math%2F0703800")
    assert response.status_code == 200
    assert response.json()["paper_id"] == "math/0703800"


@pytest.mark.parametrize("constant", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("string_payload", [False, True])
def test_nonfinite_payload_is_rejected_before_persistence(client, constant, string_payload):
    http, app = client
    predictions = http.get("/api/sample").json()["predictions"]
    predictions[0]["questions"][0]["answer_id"] = constant
    if string_payload:
        predictions = json.dumps(predictions)
    body = {"name": "invalid numeric JSON", "scope": "demo", "predictions": predictions}
    response = http.post("/api/runs", headers=HEADERS, content=json.dumps(body))
    assert 400 <= response.status_code < 500, response.text
    assert app.state.store.count() == 0
    assert http.get("/api/runs").json()["items"] == []


def test_nonfinite_validation_error_does_not_break_error_response(client):
    http, app = client
    body = {"name": float("nan"), "scope": "demo", "predictions": []}
    response = http.post("/api/runs", headers=HEADERS, content=json.dumps(body))
    assert 400 <= response.status_code < 500
    assert app.state.store.count() == 0


@pytest.mark.parametrize("string_payload", [False, True])
def test_numeric_overflow_is_rejected_without_nonstandard_json_constants(client, string_payload):
    http, app = client
    predictions = http.get("/api/sample").json()["predictions"]
    predictions[0]["questions"][0]["answer_id"] = "OVERFLOW_NUMBER"
    serialized = json.dumps(predictions).replace('"OVERFLOW_NUMBER"', "1e400")
    if string_payload:
        body = json.dumps({"name": "overflow", "scope": "demo", "predictions": serialized})
    else:
        body = '{"name":"overflow","scope":"demo","predictions":' + serialized + '}'
    response = http.post("/api/runs", headers=HEADERS, content=body)
    assert 400 <= response.status_code < 500
    assert app.state.store.count() == 0


def test_duplicate_json_keys_do_not_silently_overwrite_submission(client):
    http, app = client
    body = '{"name":"first","name":"overwritten","scope":"demo","predictions":[]}'
    response = http.post("/api/runs", headers=HEADERS, content=body)
    assert 400 <= response.status_code < 500
    assert app.state.store.count() == 0


@pytest.mark.parametrize("field", ["paper_id", "expression_text", "option", "key", "name"])
def test_unpaired_unicode_surrogate_is_rejected_before_storage(client, field):
    http, app = client
    predictions = http.get("/api/sample").json()["predictions"]
    body = {"name": "unicode input", "scope": "demo", "predictions": predictions}
    if field == "paper_id":
        predictions[0]["paper_id"] = "invalid-\ud800"
    elif field == "expression_text":
        predictions[0]["questions"][0]["expression_text"] = "\ud800"
    elif field == "option":
        predictions[0]["questions"][0]["options"][0] = "\ud800"
    elif field == "key":
        predictions[0]["questions"][0]["\ud800"] = "invalid key"
    else:
        body["name"] = "\ud800"
    response = http.post("/api/runs", headers=HEADERS, content=json.dumps(body))
    assert response.status_code == 422, response.text
    assert app.state.store.count() == 0


def test_malformed_options_are_scoreable_without_server_error(client):
    http, _ = client
    predictions = http.get("/api/sample").json()["predictions"]
    predictions[0]["questions"][0]["options"] = {"unexpected": "object"}
    response = http.post("/api/runs", headers=HEADERS, json={"name": "bad choices", "scope": "demo", "predictions": predictions})
    assert response.status_code == 201
    report = response.json()["report"]
    assert report["counts"]["predicted"] == 9
    item = report["papers"][0]["items"][0]
    assert item["structure_valid"] is False
    assert item["quality"]["q"] == 0
    assert item["options"] == {"unexpected": "object"}


def test_public_access_allows_remote_upload_and_review(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path, public_access=True, host="0.0.0.0"))
    with TestClient(app, client=("192.0.2.12", 54321)) as http:
        assert http.get("/api/session").json()["can_manage"] is True
        sample = http.get("/api/sample").json()["predictions"]
        response = http.post("/api/runs", headers=HEADERS, json={"name": "public team test", "scope": "demo", "predictions": sample})
        assert response.status_code == 201
        run = response.json()
        paper = run["report"]["papers"][0]
        item = paper["items"][0]
        result = http.post(f"/api/runs/{run['id']}/reviews", headers=HEADERS, json={
            "paper_id": paper["paper_id"], "question_id": item["question_id"],
            "accepted_option_ids": [item["answer_id"]],
            "rubric": {"plausibility": 2, "learning_alignment": 2, "distinctness": 2, "no_cues": 2},
            "gates": {"context_sufficient": True, "no_specialist_knowledge": True, "no_answer_leak": True},
            "rationale": "public manual review fixture", "reviewer": "test reviewer",
        })
        assert result.status_code == 200


def test_failed_job_does_not_release_guard_with_live_sibling_calls(tmp_path, monkeypatch):
    state = {"active": 0, "calls": 0}
    lock = threading.Lock()
    failure_started = threading.Event()
    sibling_started = threading.Event()
    release_siblings = asyncio.Event()

    async def unexpected_judge(item, config):
        with lock:
            state["active"] += 1
            state["calls"] += 1
        try:
            if item["expression_text"] == "identical to":
                failure_started.set()
                raise RuntimeError("simulated unexpected adapter failure")
            sibling_started.set()
            await release_siblings.wait()
            return quality_from_review(item, {
                "accepted_option_ids": [item["answer_id"]],
                "rubric": {"plausibility": 2, "learning_alignment": 2, "distinctness": 2, "no_cues": 2},
                "gates": {"context_sufficient": True, "no_specialist_knowledge": True, "no_answer_leak": True},
                "rationale": "mock completed", "reviewer": "test reviewer",
            })
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr("paperlingo.app.judge_item", unexpected_judge)
    app = create_app(Settings(runtime_dir=tmp_path, judge_base_url="http://localhost:1234/v1", judge_model="fixture", judge_concurrency=2))
    with TestClient(app) as http:
        sample = http.get("/api/sample").json()["predictions"]
        run = http.post("/api/runs", headers=HEADERS, json={"name": "job lifecycle", "scope": "demo", "predictions": sample}).json()
        assert run["status"] == "queued"
        assert failure_started.wait(timeout=3)
        assert sibling_started.wait(timeout=3)
        assert http.portal.call(lambda: run["id"] in app.state.jobs)
        with lock:
            assert state["active"] > 0
        # A failing sibling must not open a second job while others can still write.
        duplicate = http.post(f'/api/runs/{run["id"]}/judge', headers=HEADERS)
        assert duplicate.status_code == 200
        assert duplicate.json()["status"] == "judging"
        assert http.get(f'/api/runs/{run["id"]}').json()["judge_meta"]["job_id"] == run["judge_meta"]["job_id"]
        http.portal.call(release_siblings.set)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if http.portal.call(lambda: not app.state.jobs):
                break
            time.sleep(0.005)
        assert http.portal.call(lambda: not app.state.jobs)
        with lock:
            assert state["active"] == 0, "Global job guard was released while a sibling judge could still write results"
            assert state["calls"] == 9
        assert http.get(f'/api/runs/{run["id"]}').json()["status"] == "error"
