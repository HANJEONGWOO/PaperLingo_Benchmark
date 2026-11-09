import copy
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from paperlingo.app import create_app
from paperlingo.config import Settings, ROOT

HEADERS = {"X-Requested-With": "PaperLingo"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as c:
        yield c


def create_demo(client):
    sample = client.get("/api/sample").json()
    result = client.post("/api/runs", headers=HEADERS, json={"name": "통합 검사", "scope": "demo", "predictions": sample["predictions"]})
    assert result.status_code == 201, result.text
    return result.json()


def create_error_fixture(client):
    """Keep adversarial coverage separate from the complete public demo."""
    sample = copy.deepcopy(client.get("/api/sample").json()["predictions"])
    selected = {
        "0911.3369": ["identical to", "a function of"],
        "1109.3182": ["consistent with"],
    }
    for paper in sample:
        by_expression = {question["expression_text"]: question for question in paper["questions"]}
        paper["questions"] = [by_expression[text] for text in selected.get(paper["paper_id"], [])]
        if paper["paper_id"] == "0911.3369":
            bad = paper["questions"][1]
            bad["options"][(bad["answer_id"] + 1) % 4] = bad["options"][bad["answer_id"]]
    result = client.post("/api/runs", headers=HEADERS, json={
        "name": "명시적 오류 회귀 입력", "scope": "demo", "predictions": sample,
    })
    assert result.status_code == 201, result.text
    run = result.json()
    items = [item for paper in run["report"]["papers"] for item in paper["items"]]
    assert len(items) == 3
    assert sum(item["structure_valid"] for item in items) == 2
    assert run["report"]["counts"]["gold"] == 9
    assert run["report"]["counts"]["matched"] == 3
    assert run["is_demo"] is False
    return run


def review_payload(paper, item, **updates):
    result = {"paper_id": paper["paper_id"], "question_id": item["question_id"], "accepted_option_ids": [item["answer_id"]], "rubric": {"plausibility": 2, "learning_alignment": 2, "distinctness": 2, "no_cues": 2}, "gates": {"context_sufficient": True, "no_specialist_knowledge": True, "no_answer_leak": True}, "rationale": "문맥과 전치사 결합을 검토했습니다.", "reviewer": "테스트 검수자"}
    result.update(updates)
    return result


def test_dataset_and_sample_are_real(client):
    config = client.get("/api/config").json()
    assert config["dataset"]["paper_count"] == 1490
    assert config["dataset"]["expression_count"] == 6188
    assert config["judge"]["configured"] is False
    assert client.get("/api/papers?q=0911.3369").json()["total"] == 1
    assert client.get("/api/papers/0911.3369").json()["expressions"]
    sample = client.get("/api/sample?download=1")
    assert sample.status_code == 200
    assert "attachment" in sample.headers["content-disposition"]
    assert all("questions" in json.loads(line) for line in sample.text.splitlines())


def test_downloaded_compact_sample_is_the_public_contract(client):
    downloaded = client.get("/api/sample?download=1")
    predictions = [json.loads(line) for line in downloaded.text.splitlines()]
    schema = client.get("/api/schema").json()
    assert schema["required_question_fields"] == ["expression_text", "options", "answer_id"]
    assert schema["question_fields"]["answer_id"]["minimum"] == 0
    assert schema["question_fields"]["answer_id"]["maximum"] == 3
    for paper in predictions:
        assert len(paper["questions"]) <= 5
        for question in paper["questions"]:
            assert set(question) == {"expression_text", "options", "answer_id"}
            assert all(isinstance(option, str) for option in question["options"])
            assert type(question["answer_id"]) is int
    created = client.post("/api/runs", headers=HEADERS, json={"name": "간소화 예제", "scope": "demo", "predictions": downloaded.text})
    assert created.status_code == 201, created.text
    run = created.json()
    assert run["is_demo"] is True
    report = run["report"]
    assert [len(paper["items"]) for paper in report["papers"]] == [4, 5, 0]
    assert report["counts"]["gold"] == 9
    assert report["counts"]["predicted"] == 9
    assert report["counts"]["matched"] == 9
    for metric in ("precision_pct", "recall_pct", "f1_pct", "structural_validity_pct"):
        assert report["metrics"][metric] == 100
    assert all(item["structure_valid"] for paper in report["papers"] for item in paper["items"])
    item = run["report"]["papers"][0]["items"][0]
    assert item["structure_valid"] is True
    assert item["question_id"]
    assert "identical [BLANK]" in item["context"]


def test_compact_nonfirst_answer_supports_review_and_export(client):
    predictions = client.get("/api/sample").json()["predictions"]
    question = predictions[0]["questions"][0]
    question["options"] = ["with", "of", "to", "for"]
    question["answer_id"] = 2
    paper_id = predictions[0]["paper_id"]
    created = client.post("/api/runs", headers=HEADERS, json={"name": "세 번째 정답", "scope": "demo", "predictions": [{"paper_id": paper_id, "questions": [question]}]})
    assert created.status_code == 201, created.text
    run = created.json()
    paper = run["report"]["papers"][0]
    item = paper["items"][0]
    assert item["answer_id"] == "C"
    assert item["options"][2] == {"id": "C", "text": "to"}
    reviewed = client.post(f'/api/runs/{run["id"]}/reviews', headers=HEADERS, json=review_payload(paper, item))
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "complete"
    exported = client.get(f'/api/runs/{run["id"]}/export').json()
    assert exported["review_history"][0]["quality"]["accepted_option_ids"] == ["C"]
    assert exported["report"]["metrics"]["quality_all_pct"] == 100


def test_legacy_upload_and_saved_record_remain_reviewable(client):
    from paperlingo.dataset import Dataset
    dataset = Dataset(ROOT / "data")
    created = client.post("/api/runs", headers=HEADERS, json={"name": "이전 형식", "scope": "demo", "predictions": dataset.demo_predictions()})
    assert created.status_code == 201, created.text
    run = created.json()
    assert run["is_demo"] is True
    assert run["report"]["counts"]["gold"] == 9
    assert run["report"]["counts"]["matched"] == 9
    assert run["report"]["metrics"]["structural_validity_pct"] == 100
    paper = run["report"]["papers"][0]
    item = paper["items"][0]
    assert item["question_id"] == "demo-0911.3369-valid"
    result = client.post(f'/api/runs/{run["id"]}/reviews', headers=HEADERS, json=review_payload(paper, item))
    assert result.status_code == 200
    assert client.get(f'/api/runs/{run["id"]}').json()["report"]["papers"][0]["items"][0]["quality"]["source"] == "human"


def test_review_end_to_end_and_audit_export(client):
    run = create_demo(client)
    assert run["status"] == "awaiting_review"
    assert run["report"]["metrics"]["final_score"] is None
    assert run["report"]["metrics"]["precision_pct"] == 100
    for paper in run["report"]["papers"]:
        for item in paper["items"]:
            assert item["structure_valid"] is True
            result = client.post(f'/api/runs/{run["id"]}/reviews', headers=HEADERS, json=review_payload(paper, item))
            assert result.status_code == 200, result.text
    saved = client.get(f'/api/runs/{run["id"]}').json()
    assert saved["status"] == "complete"
    assert saved["report"]["metrics"]["final_score"] == 100
    assert saved["report"]["metrics"]["quality_all_pct"] == 100
    assert saved["report"]["metrics"]["quality_matched_pct"] == 100
    exported = client.get(f'/api/runs/{run["id"]}/export').json()
    assert len(exported["review_history"]) == 9
    assert all(h["quality"]["reviewer"] == "테스트 검수자" for h in exported["review_history"])
    csv = client.get(f'/api/runs/{run["id"]}/export?format=csv')
    assert csv.status_code == 200 and "quality_status" in csv.text
    assert len(client.get("/api/runs").json()["items"]) == 1


def test_complete_demo_with_temporary_quality_scores_100(client):
    run = create_demo(client)
    response = client.post(f'/api/runs/{run["id"]}/skip-judge', headers=HEADERS)
    assert response.status_code == 200, response.text
    completed = response.json()
    assert completed["status"] == "complete"
    assert completed["report"]["score_mode"] == "temporary"
    assert completed["report"]["counts"]["quality_temporary"] == 9
    for metric in ("precision_pct", "recall_pct", "f1_pct", "quality_matched_pct", "final_score"):
        assert completed["report"]["metrics"][metric] == 100
    history = client.get(f'/api/runs/{run["id"]}/export').json()["review_history"]
    assert len(history) == 9
    assert all(entry["quality"]["source"] == "temporary" for entry in history)
    assert all(entry["quality"]["option_assessments"] == [] for entry in history)


def test_missing_scope_is_not_inferred_from_submission(client):
    sample = client.get("/api/sample").json()
    result = client.post("/api/runs", headers=HEADERS, json={"name": "불완전 제출", "scope": "train", "predictions": sample["predictions"]})
    assert result.status_code == 201
    report = result.json()["report"]
    dataset = client.app.state.dataset
    assert report["counts"]["gold"] == sum(len(dataset.labels[pid]) for pid in dataset.splits["train"])
    assert report["counts"]["missing_papers"] == 1040
    assert report["metrics"]["coverage_pct"] < 1


def test_bad_json_and_duplicate_paper_reject_without_run(client):
    bad = client.post("/api/runs", headers=HEADERS, json={"name": "bad", "scope": "demo", "predictions": '{"paper_id":'})
    assert bad.status_code == 422
    sample = client.get("/api/sample").json()["predictions"]
    bad = client.post("/api/runs", headers=HEADERS, json={"name": "bad", "scope": "demo", "predictions": sample + sample})
    assert bad.status_code == 422
    assert not client.get("/api/runs").json()["items"]


def test_structural_invalid_cannot_be_overridden(client):
    run = create_error_fixture(client)
    paper, item = next((p, i) for p in run["report"]["papers"] for i in p["items"] if not i["structure_valid"])
    result = client.post(f'/api/runs/{run["id"]}/reviews', headers=HEADERS, json=review_payload(paper, item))
    assert result.status_code == 422


def test_manual_ambiguity_gates_to_zero(client):
    run = create_demo(client)
    paper, item = next((p, i) for p in run["report"]["papers"] for i in p["items"] if i["structure_valid"])
    result = client.post(f'/api/runs/{run["id"]}/reviews', headers=HEADERS, json=review_payload(paper, item, accepted_option_ids=["A", "B"]))
    assert result.status_code == 200
    quality = result.json()["report"]["papers"][0]["items"][0]["quality"]
    assert quality["q"] == 0


def test_manual_answer_correction_recomputes_zero_quality_and_keeps_audit(client):
    run = create_error_fixture(client)
    valid = [(p, i) for p in run["report"]["papers"] for i in p["items"] if i["structure_valid"]]
    assert len(valid) == 2
    # Reproduce the reported state: all four rubric scores are 2 and all gates
    # are true, but the sole accepted option differs from the generator's key.
    for (paper, item), selected in zip(valid, ("B", "D")):
        assert item["answer_id"] == "A"
        result = client.post(f'/api/runs/{run["id"]}/reviews', headers=HEADERS,
                             json=review_payload(paper, item, accepted_option_ids=[selected]))
        assert result.status_code == 200, result.text
    zero = client.get(f'/api/runs/{run["id"]}').json()
    assert zero["status"] == "complete"
    assert zero["report"]["metrics"]["quality_matched_pct"] == 0
    assert zero["report"]["metrics"]["final_score"] == 0
    # Correcting the accepted options must update the stored aggregate, while
    # preserving the intentional structural failure and previous review history.
    for paper, item in valid:
        result = client.post(f'/api/runs/{run["id"]}/reviews', headers=HEADERS,
                             json=review_payload(paper, item))
        assert result.status_code == 200, result.text
    updated = client.get(f'/api/runs/{run["id"]}/export').json()
    assert updated["status"] == "complete"
    assert updated["report"]["metrics"]["quality_matched_pct"] == pytest.approx(200 / 3)
    assert updated["report"]["metrics"]["final_score"] == pytest.approx(200 / 9)
    history = updated["review_history"]
    assert len(history) == 4
    assert [entry["previous_quality"]["accepted_option_ids"] for entry in history[2:]] == [["B"], ["D"]]
    assert all(entry["quality"]["q"] == 1 for entry in history[2:])


def test_no_llm_call_without_configuration(client):
    run = create_demo(client)
    result = client.post(f'/api/runs/{run["id"]}/judge', headers=HEADERS)
    assert result.status_code == 409
    assert client.get(f'/api/runs/{run["id"]}').json()["report"]["metrics"]["final_score"] is None


def test_auth_session_csrf_and_logout(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path, password="test-only-password"))) as client:
        assert client.get("/api/session").json()["auth_required"] is True
        assert client.get("/api/config").status_code == 401
        assert client.post("/api/login", json={"password": "test-only-password"}).status_code == 403
        assert client.post("/api/login", headers={**HEADERS, "Origin": "https://attacker.invalid"}, json={"password": "test-only-password"}).status_code == 403
        login = client.post("/api/login", headers=HEADERS, json={"password": "test-only-password"})
        assert login.status_code == 200
        assert "HttpOnly" in login.headers["set-cookie"]
        assert client.get("/api/config").status_code == 200
        assert client.post("/api/logout", headers=HEADERS).status_code == 200
        assert client.get("/api/config").status_code == 401


def test_public_read_and_private_write(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path, password="test-only", public_read=True))) as client:
        assert client.get("/api/config").status_code == 200
        sample = client.get("/api/sample").json()["predictions"]
        assert client.post("/api/runs", headers=HEADERS, json={"name": "blocked", "scope": "demo", "predictions": sample}).status_code == 401


def test_explicit_public_access_allows_remote_submission(tmp_path):
    settings = Settings(runtime_dir=tmp_path, public_access=True, host="0.0.0.0")
    with TestClient(create_app(settings), client=("192.0.2.10", 12345)) as client:
        assert client.get("/api/session").json()["can_manage"] is True
        assert client.get("/api/session").json()["auth_required"] is False
        run = create_demo(client)
        assert run["id"]


def test_remote_client_cannot_use_no_password_mode(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path)), client=("192.0.2.10", 12345)) as client:
        assert client.get("/api/config").status_code == 401


def test_password_login_throttles(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path, password="test-only"))) as client:
        for _ in range(5):
            assert client.post("/api/login", headers=HEADERS, json={"password": "wrong"}).status_code == 401
        assert client.post("/api/login", headers=HEADERS, json={"password": "wrong"}).status_code == 429


def test_llm_background_uses_mock_and_never_returns_key(tmp_path, monkeypatch):
    from paperlingo.judge import quality_from_review

    async def mock_judge(item, config):
        return quality_from_review(item, {"accepted_option_ids": [item["answer_id"]], "rubric": {"plausibility": 1, "learning_alignment": 2, "distinctness": 2, "no_cues": 2}, "gates": {"context_sufficient": True, "no_specialist_knowledge": True, "no_answer_leak": True}, "rationale": "모의 응답", "reviewer": "test-only"})

    monkeypatch.setattr("paperlingo.app.judge_item", mock_judge)
    cfg = Settings(runtime_dir=tmp_path, judge_base_url="https://judge.invalid/v1", judge_model="mock-model", judge_api_key="secret-never-return")
    with TestClient(create_app(cfg)) as client:
        assert "secret-never-return" not in client.get("/api/config").text
        run = create_demo(client)
        for _ in range(50):
            result = client.get(f'/api/runs/{run["id"]}').json()
            if result["status"] not in {"queued", "judging"}:
                break
            time.sleep(0.01)
        assert result["status"] == "complete"
        assert result["report"]["metrics"]["final_score"] is not None
        assert "secret-never-return" not in json.dumps(result)


def test_csv_formula_strings_are_escaped():
    from paperlingo.app import csv_value
    assert csv_value("=SUM(1,2)") == "'=SUM(1,2)"
    assert csv_value("normal expression") == "normal expression"


def test_interrupted_job_is_recovered(tmp_path):
    settings = Settings(runtime_dir=tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        run = create_demo(client)
        run["status"] = "judging"
        app.state.store.save(run)
    with TestClient(create_app(settings)) as client:
        recovered = client.get(f'/api/runs/{run["id"]}').json()
        assert recovered["status"] == "awaiting_review"
        assert recovered["judge_meta"]["interrupted"] is True
