import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from paperlingo.judge import (
    QUALITY_POLICY_VERSION, TEMPORARY_QUALITY_POLICY_VERSION,
    JudgeConfig, _response_identity, judge_item, quality_from_review,
    strict_json_loads, temporary_quality,
)


@pytest.fixture
def item():
    return {
        "question_id": "q1", "structure_valid": True,
        "context": "The outcome depends [BLANK] the method.",
        "expression_text": "depends on", "learning_point": "SECRET_TARGET",
        "options": [
            {"id": "A", "text": "on"}, {"id": "B", "text": "at"},
            {"id": "C", "text": "for"}, {"id": "D", "text": "to"},
        ],
        "answer_id": "A", "abstract": "SECRET_SOURCE", "gold_expressions": ["SECRET_GOLD"],
    }


@pytest.fixture
def llm_review():
    return {
        "option_assessments": [
            {"id": option, "verdict": "acceptable" if option == "A" else "incorrect", "reason": "Concrete linguistic explanation."}
            for option in "ABCD"
        ],
        "rubric": {"plausibility": 2, "learning_alignment": 2, "distinctness": 1, "no_cues": 2},
        "gates": {"context_sufficient": True, "no_specialist_knowledge": True, "no_answer_leak": True},
        "rationale": "Three credible preposition confusions; two distractors overlap in rationale.",
    }


@pytest.fixture
def human_review(llm_review):
    return {
        "accepted_option_ids": ["A"], "rubric": llm_review["rubric"],
        "gates": llm_review["gates"], "rationale": "검토 근거", "reviewer": "정우",
    }


def config(**kwargs):
    return JudgeConfig("https://example.test/v1", "secret-api-key", "explicit-test-model", max_retries=0, **kwargs)


def mock_transport(monkeypatch, handler):
    original_client = httpx.AsyncClient

    def factory(**kwargs):
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("paperlingo.judge.httpx.AsyncClient", factory)


def response_for(payload, **extra):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}],
        **extra,
    })


@pytest.mark.parametrize("distinctness,expected", [(1, 7 / 8), (2, 1)])
def test_manual_quality_uses_same_code_gate(item, human_review, distinctness, expected):
    human_review["rubric"]["distinctness"] = distinctness
    result = quality_from_review(item, human_review)
    assert result["q"] == expected
    assert result["reviewer"] == "정우"
    assert result["source"] == "human"
    assert len(result["option_assessments"]) == 4
    assert result["accepted_option_ids"] == ["A"]
    assert result["review_recommended"] is False
    assert result["scoring_policy"] == QUALITY_POLICY_VERSION


def test_ambiguity_is_completed_zero_not_high_rubric(item, llm_review):
    llm_review["option_assessments"][1]["verdict"] = "acceptable"
    result = quality_from_review(item, llm_review, source="llm")
    assert result["status"] == "complete"
    assert result["q"] == 0
    assert result["accepted_option_ids"] == ["A", "B"]


@pytest.mark.parametrize("source", ["human", "llm"])
def test_wrong_unique_key_is_zero_even_with_max_rubric(item, llm_review, human_review, source):
    item["answer_id"] = "B"
    review = human_review if source == "human" else llm_review
    review["rubric"] = {criterion: 2 for criterion in review["rubric"]}
    result = quality_from_review(item, review, source)
    assert result["accepted_option_ids"] == ["A"]
    assert result["status"] == "complete"
    assert result["q"] == 0


@pytest.mark.parametrize("source", ["human", "llm"])
def test_no_acceptable_answer_is_zero(item, llm_review, human_review, source):
    if source == "human":
        human_review["accepted_option_ids"] = []
        review = human_review
    else:
        llm_review["option_assessments"][0]["verdict"] = "incorrect"
        review = llm_review
    review["rubric"] = {criterion: 2 for criterion in review["rubric"]}
    result = quality_from_review(item, review, source)
    assert result["accepted_option_ids"] == []
    assert result["status"] == "complete"
    assert result["q"] == 0


@pytest.mark.parametrize("gate", ["context_sufficient", "no_specialist_knowledge", "no_answer_leak"])
def test_gate_failure_zero(item, human_review, gate):
    human_review["gates"][gate] = False
    assert quality_from_review(item, human_review)["q"] == 0


@pytest.mark.parametrize("uncertain_option", [0, 1])
def test_uncertainty_completes_conservatively_and_preserves_evidence(item, llm_review, uncertain_option):
    llm_review["rubric"] = {criterion: 2 for criterion in llm_review["rubric"]}
    llm_review["option_assessments"][uncertain_option]["verdict"] = "uncertain"
    result = quality_from_review(item, llm_review, "llm")
    assert result["status"] == "complete"
    assert result["q"] == 0
    assert result["review_recommended"] is True
    assert result["scoring_policy"] == "auto-conservative-v1" == QUALITY_POLICY_VERSION
    assert result["accepted_option_ids"] == ([] if uncertain_option == 0 else ["A"])
    assert result["option_assessments"] == llm_review["option_assessments"]
    assert result["rationale"] == llm_review["rationale"]


def test_optional_human_review_can_replace_conservative_result(item, llm_review, human_review):
    llm_review["option_assessments"][1]["verdict"] = "uncertain"
    item["quality"] = quality_from_review(item, llm_review, "llm")
    result = quality_from_review(item, human_review)
    assert result["status"] == "complete"
    assert result["q"] == 7 / 8
    assert result["source"] == "human"
    assert result["review_recommended"] is False


def test_temporary_credit_is_explicit_and_contains_no_fabricated_review(item):
    before = json.loads(json.dumps(item))
    result = temporary_quality(item)
    assert result["status"] == "complete"
    assert result["source"] == "temporary"
    assert result["q"] == 1
    assert result["accepted_option_ids"] == []
    assert result["option_assessments"] == []
    assert result["rubric"] is None
    assert result["gates"] is None
    assert result["reviewer"] is None
    assert result["review_recommended"] is True
    assert result["scoring_policy"] == "temporary-full-credit-v1" == TEMPORARY_QUALITY_POLICY_VERSION
    assert "자동 검수 생략" in result["rationale"]
    assert "실제 검수 아님" in result["rationale"]
    assert item == before


@pytest.mark.parametrize("structure_valid", [False, None, 1, "true"])
def test_temporary_credit_cannot_bypass_structure_failures(item, structure_valid):
    item["structure_valid"] = structure_valid
    with pytest.raises(ValueError, match="Structurally invalid"):
        temporary_quality(item)


@pytest.mark.parametrize("value", [True, False, 2.0, "2", 3, -1, None])
def test_rubric_strict_ints(item, human_review, value):
    human_review["rubric"]["plausibility"] = value
    with pytest.raises(ValueError):
        quality_from_review(item, human_review)


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_gates_strict_bools(item, human_review, value):
    human_review["gates"]["no_answer_leak"] = value
    with pytest.raises(ValueError):
        quality_from_review(item, human_review)


@pytest.mark.parametrize("field", ["rationale", "reviewer"])
def test_human_identity_and_reason_required(item, human_review, field):
    human_review[field] = "  "
    with pytest.raises(ValueError):
        quality_from_review(item, human_review)


def test_no_overriding_structure_failure(item, human_review):
    item["structure_valid"] = False
    with pytest.raises(ValueError):
        quality_from_review(item, human_review)


@pytest.mark.parametrize("accepted", [["A", "A"], ["Z"], [None], "A"])
def test_manual_accepted_ids_strict(item, human_review, accepted):
    human_review["accepted_option_ids"] = accepted
    with pytest.raises(ValueError):
        quality_from_review(item, human_review)


def test_all_options_must_be_assessed_exactly_once(item, llm_review):
    llm_review["option_assessments"][1]["id"] = "A"
    with pytest.raises(ValueError):
        quality_from_review(item, llm_review, "llm")


def test_llm_cannot_supply_q_or_accepted_ids(item, llm_review):
    llm_review["q"] = 1
    with pytest.raises(ValueError):
        quality_from_review(item, llm_review, "llm")


def test_rejects_extra_nested_fields(item, llm_review):
    llm_review["option_assessments"][0]["score"] = 1
    with pytest.raises(ValueError):
        quality_from_review(item, llm_review, "llm")


@pytest.mark.asyncio
async def test_api_blindness_order_stability_and_metadata(monkeypatch, item, llm_review):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer secret-api-key"
        return response_for(llm_review, usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "secret": "discard"})

    mock_transport(monkeypatch, handler)
    first = await judge_item(item, config())
    second = await judge_item(item, config())
    candidate = json.loads(requests[0]["messages"][1]["content"])
    assert set(candidate) == {"learner_level", "context", "options"}
    assert candidate == json.loads(requests[1]["messages"][1]["content"])
    assert "SECRET_" not in json.dumps(requests)
    assert "answer_id" not in candidate
    assert first["q"] == 7 / 8
    assert first["metadata"]["usage"] == {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
    assert first["metadata"]["option_order"] == second["metadata"]["option_order"]
    assert len(first["metadata"]["prompt_sha256"]) == 64
    assert "secret-api-key" not in json.dumps(first)


@pytest.mark.asyncio
async def test_uncertain_model_response_finishes_with_zero_not_provider_error(monkeypatch, item, llm_review):
    llm_review["option_assessments"][1]["verdict"] = "uncertain"
    mock_transport(monkeypatch, lambda req: response_for(llm_review))
    result = await judge_item(item, config())
    assert result["status"] == "complete"
    assert result["source"] == "llm"
    assert result["q"] == 0
    assert result["review_recommended"] is True
    assert result["scoring_policy"] == QUALITY_POLICY_VERSION
    assert result["option_assessments"][1]["verdict"] == "uncertain"
    assert result["metadata"]["attempts"] == 1
    assert "error_code" not in result


@pytest.mark.asyncio
async def test_provider_error_sanitized(monkeypatch, item):
    mock_transport(monkeypatch, lambda req: httpx.Response(401, json={"error": "secret-api-key/provider private body"}))
    result = await judge_item(item, config())
    assert result["status"] == "error"
    assert result["q"] is None
    assert result["error_code"] == "provider_error"
    assert "secret-api-key" not in json.dumps(result)
    assert "private body" not in json.dumps(result)


@pytest.mark.asyncio
async def test_transport_error_sanitized(monkeypatch, item):
    def handler(request):
        raise httpx.ConnectError("secret-api-key private exception", request=request)

    mock_transport(monkeypatch, handler)
    result = await judge_item(item, config())
    assert result["status"] == "error"
    assert result["q"] is None
    assert result["error_code"] == "transport_error"
    assert "secret-api-key" not in json.dumps(result)


@pytest.mark.asyncio
async def test_invalid_schema_error_not_zero(monkeypatch, item, llm_review):
    llm_review["gates"]["context_sufficient"] = "yes"
    mock_transport(monkeypatch, lambda req: response_for(llm_review))
    result = await judge_item(item, config())
    assert result["status"] == "error"
    assert result["q"] is None
    assert result["error_code"] == "invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, [], {"choices": [None]}, {"choices": ["x"]}])
async def test_malformed_provider_envelope(monkeypatch, item, body):
    mock_transport(monkeypatch, lambda req: httpx.Response(200, json=body))
    result = await judge_item(item, config())
    assert result["error_code"] == "invalid_response"
    assert result["q"] is None


@pytest.mark.asyncio
async def test_json_mode_compatibility_fallback(monkeypatch, item, llm_review):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"param": "response_format", "message": "response_format is not supported"}})
        return response_for(llm_review)

    mock_transport(monkeypatch, handler)
    result = await judge_item(item, config())
    assert result["status"] == "complete"
    assert len(requests) == 2
    assert result["metadata"]["json_mode_fallback"] is True


@pytest.mark.asyncio
async def test_redirect_not_followed(monkeypatch, item):
    visited = []

    def handler(request):
        visited.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://elsewhere.test/collect"})

    mock_transport(monkeypatch, handler)
    result = await judge_item(item, config())
    assert result["error_code"] == "provider_error"
    assert visited == ["https://example.test/v1/chat/completions"]


@pytest.mark.asyncio
async def test_truncated_completion_is_not_graded(monkeypatch, item, llm_review):
    mock_transport(monkeypatch, lambda req: httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(llm_review)}, "finish_reason": "length"}],
    }))
    result = await judge_item(item, config())
    assert result["status"] == "error"
    assert result["q"] is None


@pytest.mark.asyncio
async def test_retry_transport_and_rate_limit_is_bounded(monkeypatch, item, llm_review):
    attempts = []

    async def no_delay(seconds):
        pass

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("private provider data", request=request)
        if len(attempts) == 2:
            return httpx.Response(429, json={"error": "private provider data"})
        return response_for(llm_review)

    mock_transport(monkeypatch, handler)
    monkeypatch.setattr("paperlingo.judge.asyncio.sleep", no_delay)
    result = await judge_item(item, JudgeConfig("https://example.test/v1", "key", "test", max_retries=2))
    assert result["status"] == "complete"
    assert result["metadata"]["attempts"] == 3
    assert len(attempts) == 3


@pytest.mark.parametrize("content", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_strict_json_rejects_nonstandard_or_duplicate_values(content):
    with pytest.raises(ValueError):
        strict_json_loads(content)


def test_key_hidden_in_config_repr():
    assert "secret-api-key" not in repr(config())
    assert config().endpoint == "https://example.test/v1/chat/completions"
    assert JudgeConfig("http://localhost:1234/v1/chat/completions", "", "local-model").endpoint.endswith("/v1/chat/completions")


def test_response_identity_is_bounded_and_restricted():
    assert _response_identity({"model": "Qwen/Qwen3-8B", "system_fingerprint": "local-123abc"}) == {
        "response_model": "Qwen/Qwen3-8B", "system_fingerprint": "local-123abc",
    }
    assert _response_identity({"model": "private\nprovider text", "system_fingerprint": "a" * 201}) == {}


@pytest.mark.parametrize("url", ["file:///secret", "https://name:pass@example.test/v1", "https://example.test/v1?key=secret", "https://example.test/v1#secret"])
def test_no_credentials_in_endpoint(url):
    with pytest.raises(ValueError):
        JudgeConfig(url, "key", "model")


def local_busy_response():
    return httpx.Response(503, json={"error": {
        "type": "local_validation",
        "message": "The local model is busy; retry after the current request.",
    }})


@pytest.fixture
def fake_retry_clock(monkeypatch):
    clock = {"now": 100.0, "sleeps": []}

    async def advance(seconds):
        clock["sleeps"].append(seconds)
        clock["now"] += seconds

    # Replace this module's clock object rather than the event loop's clock.
    monkeypatch.setattr("paperlingo.judge.time", SimpleNamespace(monotonic=lambda: clock["now"]))
    monkeypatch.setattr("paperlingo.judge.asyncio.sleep", advance)
    return clock


@pytest.mark.asyncio
async def test_local_busy_waits_for_gpu_without_consuming_regular_retries(monkeypatch, item, llm_review, fake_retry_clock):
    calls = []

    def handler(request):
        calls.append(request)
        return local_busy_response() if len(calls) <= 3 else response_for(llm_review)

    mock_transport(monkeypatch, handler)
    result = await judge_item(item, config(timeout=10))  # max_retries=0
    assert result["status"] == "complete"
    assert result["q"] == 7 / 8
    assert len(calls) == result["metadata"]["attempts"] == 4
    assert result["metadata"]["busy_retries"] == 3
    assert result["metadata"]["busy_wait_ms"] == 6000
    assert fake_retry_clock["sleeps"] == [2.0, 2.0, 2.0]


@pytest.mark.asyncio
async def test_local_busy_keeps_regular_retry_available_after_gpu_recovers(monkeypatch, item, llm_review, fake_retry_clock):
    responses = [local_busy_response(), local_busy_response(), httpx.Response(503, json={"error": "ordinary outage"}), response_for(llm_review)]
    mock_transport(monkeypatch, lambda request: responses.pop(0))
    result = await judge_item(item, JudgeConfig("https://example.test/v1", "key", "test", timeout=10, max_retries=1))
    assert result["status"] == "complete"
    assert result["metadata"]["attempts"] == 4
    assert result["metadata"]["busy_retries"] == 2
    assert fake_retry_clock["sleeps"] == [2.0, 2.0, 1]


@pytest.mark.asyncio
async def test_local_busy_deadline_returns_unresolved_provider_error(monkeypatch, item, fake_retry_clock):
    calls = []

    def handler(request):
        calls.append(request)
        return local_busy_response()

    mock_transport(monkeypatch, handler)
    result = await judge_item(item, JudgeConfig("https://example.test/v1", "key", "test", timeout=5, max_retries=5))
    assert result["status"] == "error"
    assert result["q"] is None
    assert result["error_code"] == "provider_error"
    assert result["metadata"]["http_status"] == 503
    assert result["metadata"]["busy_retries"] == 2
    assert result["metadata"]["busy_wait_ms"] == 5000
    assert result["metadata"]["latency_ms"] == 5000
    assert len(calls) == 3
    assert fake_retry_clock["sleeps"] == [2.0, 2.0, 1.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [
    (503, {"type": "server_error", "message": "The local model is busy; retry after the current request."}),
    (503, {"type": "local_validation", "message": "Another validation error."}),
    (500, {"type": "local_validation", "message": "The local model is busy; retry after the current request."}),
    (503, "ordinary provider error"),
])
async def test_other_503_or_similar_errors_keep_regular_retry_limit(monkeypatch, item, fake_retry_clock, status, error):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": error})

    mock_transport(monkeypatch, handler)
    result = await judge_item(item, JudgeConfig("https://example.test/v1", "key", "test", timeout=10, max_retries=2))
    assert result["status"] == "error"
    assert result["q"] is None
    assert result["metadata"]["http_status"] == status
    assert result["metadata"]["attempts"] == len(calls) == 3
    assert "busy_retries" not in result["metadata"]
    assert fake_retry_clock["sleeps"] == [1, 2]


@pytest.mark.asyncio
async def test_local_busy_wait_remains_cancellable(monkeypatch, item):
    mock_transport(monkeypatch, lambda request: local_busy_response())

    async def cancelled_sleep(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr("paperlingo.judge.asyncio.sleep", cancelled_sleep)
    with pytest.raises(asyncio.CancelledError):
        await judge_item(item, config())
