import argparse
import json

import httpx
import pytest

from scripts.example_generator import run, to_prediction


@pytest.fixture
def paper():
    return {"paper_id": "p1", "abstract": "α text. It depends on time. It depends on time."}


@pytest.fixture
def prediction():
    return {"paper_id": "p1", "questions": [{
        "expression_text": "depends on", "options": ["at", "on", "for", "to"], "answer_id": 1,
    }]}


def test_compact_prediction_preserves_unicode_and_repeated_source(paper, prediction):
    # Repeated occurrences are resolved by the scoring server, not by extra
    # generator fields or offsets. The submission remains exactly three fields.
    assert to_prediction(paper, prediction) == prediction
    assert set(to_prediction(paper, prediction)["questions"][0]) == {
        "expression_text", "options", "answer_id",
    }


@pytest.mark.parametrize("answer_id", [0, 1, 2, 3])
def test_every_zero_based_answer_position(paper, prediction, answer_id):
    question = prediction["questions"][0]
    question["options"][1], question["options"][answer_id] = question["options"][answer_id], question["options"][1]
    question["answer_id"] = answer_id
    assert to_prediction(paper, prediction)["questions"][0]["options"][answer_id] == "on"


@pytest.mark.parametrize("answer_id", [True, False, -1, 4, 1.0, "1", "B", None])
def test_answer_index_rejects_boolean_and_non_indices(paper, prediction, answer_id):
    prediction["questions"][0]["answer_id"] = answer_id
    with pytest.raises(ValueError, match="integer index"):
        to_prediction(paper, prediction)


@pytest.mark.parametrize("expression", ["depend on", "Depends on", "depends  on", "pends on", "", " ", None])
def test_expression_requires_exact_whole_words(paper, prediction, expression):
    prediction["questions"][0]["expression_text"] = expression
    with pytest.raises(ValueError, match="expression_text"):
        to_prediction(paper, prediction)


@pytest.mark.parametrize("expression,answer", [("condition", "on"), ("depends on", "ON"), ("depends on", "for")])
def test_answer_must_be_verbatim_whole_words_within_expression(paper, prediction, expression, answer):
    paper["abstract"] = "The " + expression + "."
    prediction["questions"][0].update(expression_text=expression, options=["at", answer, "to", "with"])
    with pytest.raises(ValueError, match="Correct option"):
        to_prediction(paper, prediction)


def test_answer_can_equal_the_entire_expression(paper, prediction):
    prediction["questions"][0].update(options=["relies at", "depends on", "dependent for", "depending to"])
    assert to_prediction(paper, prediction) == prediction


def test_later_whole_word_occurrence_is_valid(paper, prediction):
    paper["abstract"] = "predepends on time. It depends on time."
    assert to_prediction(paper, prediction) == prediction


@pytest.mark.parametrize("duplicate", ["on", "ON", " on ", "ｏｎ"])
def test_duplicate_option_texts_are_rejected(paper, prediction, duplicate):
    prediction["questions"][0]["options"][0] = duplicate
    with pytest.raises(ValueError, match="distinct"):
        to_prediction(paper, prediction)


@pytest.mark.parametrize("options", [
    ["on", "at", "to"], ["on", "at", "to", "for", "with"],
    [{"id": "A", "text": "on"}, "at", "to", "for"],
    ["on", "", "to", "for"], ["on", 1, "to", "for"], None,
])
def test_exactly_four_nonempty_string_options(paper, prediction, options):
    prediction["questions"][0]["options"] = options
    with pytest.raises(ValueError):
        to_prediction(paper, prediction)


@pytest.mark.parametrize("old_field", [
    "question_id", "expression_type", "expression_span", "context_span", "blank_span",
    "context_id", "expression_occurrence", "blank_occurrence", "blank_text", "learning_point",
])
def test_old_or_extra_question_fields_rejected(paper, prediction, old_field):
    prediction["questions"][0][old_field] = "unused"
    with pytest.raises(ValueError, match="fields"):
        to_prediction(paper, prediction)


def test_zero_to_ten_questions_allowed_and_eleven_rejected(paper, prediction):
    assert to_prediction(paper, {"paper_id": "p1", "questions": []})["questions"] == []
    prediction["questions"] *= 10
    assert len(to_prediction(paper, prediction)["questions"]) == 10
    prediction["questions"].append(prediction["questions"][0])
    with pytest.raises(ValueError, match="0–10"):
        to_prediction(paper, prediction)


def test_wrong_paper_id_rejected(paper, prediction):
    prediction["paper_id"] = "different"
    with pytest.raises(ValueError, match="paper_id"):
        to_prediction(paper, prediction)


def configure_mock(monkeypatch, tmp_path, paper, generated):
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps({
        **paper, "paper_name": "Research example", "domain": {"code": "test"},
        "expressions": ["SECRET_REFERENCE_LABEL"], "gold_expressions": ["SECRET_GOLD"],
        "questions": [{"secret": "SECRET_QUESTION"}],
    }), encoding="utf-8")
    args = argparse.Namespace(input=source, output=tmp_path / "predictions.jsonl", limit=1)
    monkeypatch.setenv("GENERATOR_BASE_URL", "http://example.test/v1")
    monkeypatch.setenv("GENERATOR_MODEL", "local-test")
    monkeypatch.setenv("GENERATOR_API_KEY", "")
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "local-test", "system_fingerprint": "local-test-revision",
            "choices": [{"message": {"content": json.dumps(generated)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        })

    original_client = httpx.AsyncClient

    def factory(**kwargs):
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("scripts.example_generator.httpx.AsyncClient", factory)
    return args, captured


@pytest.mark.asyncio
async def test_cli_writes_compact_jsonl_and_metadata_without_reference_leak(monkeypatch, tmp_path, paper, prediction):
    args, captured = configure_mock(monkeypatch, tmp_path, paper, prediction)
    await run(args)
    assert json.loads(args.output.read_text()) == prediction
    assert len(captured) == 1
    supplied = json.loads(captured[0]["messages"][1]["content"])
    assert set(supplied) == {"paper_id", "paper_name", "domain", "abstract"}
    assert supplied["abstract"] == paper["abstract"]
    assert "SECRET_" not in json.dumps(captured)
    assert "contexts" not in supplied
    metadata = json.loads((tmp_path / "predictions.jsonl.meta.json").read_text())
    assert metadata["paper_count"] == 1
    assert len(metadata["prompt_sha256"]) == 64
    assert metadata["papers"][0]["response_model"] == "local-test"
    assert metadata["papers"][0]["system_fingerprint"] == "local-test-revision"
    assert metadata["papers"][0]["usage"]["completion_tokens"] == 50


@pytest.mark.asyncio
async def test_invalid_model_output_is_not_silently_repaired_or_saved(monkeypatch, tmp_path, paper, prediction):
    prediction["questions"][0]["answer_id"] = True
    args, captured = configure_mock(monkeypatch, tmp_path, paper, prediction)
    with pytest.raises(ValueError, match="integer index"):
        await run(args)
    assert len(captured) == 1
    assert not args.output.exists()
    assert not (tmp_path / "predictions.jsonl.meta.json").exists()
