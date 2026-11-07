import json

import httpx
import pytest

from paperlingo.judge import JudgeConfig, _response_identity, judge_item, quality_from_review, strict_json_loads
from scripts.example_generator import locate, to_prediction


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


def test_manual_quality_uses_same_code_gate(item, human_review):
    result = quality_from_review(item, human_review)
    assert result["q"] == 7 / 8
    assert result["reviewer"] == "정우"
    assert result["source"] == "human"
    assert len(result["option_assessments"]) == 4
    assert result["accepted_option_ids"] == ["A"]


def test_ambiguity_is_completed_zero_not_high_rubric(item, llm_review):
    llm_review["option_assessments"][1]["verdict"] = "acceptable"
    result = quality_from_review(item, llm_review, source="llm")
    assert result["status"] == "complete"
    assert result["q"] == 0
    assert result["accepted_option_ids"] == ["A", "B"]


def test_wrong_unique_key_is_zero(item, llm_review):
    item["answer_id"] = "B"
    assert quality_from_review(item, llm_review, "llm")["q"] == 0


def test_no_acceptable_answer_is_zero(item, llm_review):
    llm_review["option_assessments"][0]["verdict"] = "incorrect"
    assert quality_from_review(item, llm_review, "llm")["q"] == 0


@pytest.mark.parametrize("gate", ["context_sufficient", "no_specialist_knowledge", "no_answer_leak"])
def test_gate_failure_zero(item, human_review, gate):
    human_review["gates"][gate] = False
    assert quality_from_review(item, human_review)["q"] == 0


def test_uncertain_not_a_completed_zero(item, llm_review):
    llm_review["option_assessments"][1]["verdict"] = "uncertain"
    result = quality_from_review(item, llm_review, "llm")
    assert result["status"] == "needs_review"
    assert result["q"] is None


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


def test_generator_resolves_unicode_and_repeated_text():
    abstract = "α text. It depends on time. It depends on time."
    paper = {"paper_id": "p1", "abstract": abstract}
    generated = {"paper_id": "p1", "questions": [{
        "question_id": "q1", "expression_text": "depends on", "expression_type": "preposition_pattern",
        "context_id": "c3", "blank_text": "on",
        "expression_occurrence": 0, "blank_occurrence": 0,
        "options": [{"id": option_id, "text": text} for option_id, text in zip("ABCD", ["on", "at", "for", "to"])],
        "answer_id": "A", "learning_point": "depend on의 전치사",
    }]}
    output = to_prediction(paper, generated)
    question = output["questions"][0]
    start, end = question["expression_span"].values()
    assert abstract[start:end] == "depends on"
    assert start == abstract.rindex("depends on")
    start, end = question["blank_span"].values()
    assert abstract[start:end] == "on"
    assert "context_text" not in question


def test_generator_rejects_invented_or_ambiguous_occurrence():
    with pytest.raises(ValueError):
        locate("depends on", "depend on", 0, "expression_text")
    with pytest.raises(ValueError):
        locate("depends on", "depends on", 1, "expression_text")
    with pytest.raises(ValueError):
        locate("depends on", "depends on", True, "expression_text")


@pytest.mark.parametrize("duplicate", ["on", "ON", " on ", "ｏｎ"])
def test_generator_rejects_duplicate_option_texts(duplicate):
    paper = {"paper_id": "p1", "abstract": "The outcome depends on the method."}
    generated = {"paper_id": "p1", "questions": [{
        "question_id": "q1", "expression_text": "depends on", "expression_type": "preposition_pattern",
        "context_id": "c1", "blank_text": "on", "expression_occurrence": 0, "blank_occurrence": 0,
        "options": [{"id": option_id, "text": text} for option_id, text in zip("ABCD", ["on", duplicate, "for", "to"])],
        "answer_id": "A", "learning_point": "전치사 on",
    }]}
    with pytest.raises(ValueError, match="distinct"):
        to_prediction(paper, generated)
