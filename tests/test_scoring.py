from copy import deepcopy
import json

import pytest

from paperlingo.dataset import Dataset
from paperlingo.scoring import SubmissionError, evaluate, recompute


@pytest.fixture
def dataset(tmp_path):
    papers = [
        {"paper_id": "p1", "paper_name": "First", "domain": {"code": "x"}, "abstract": "결과 😀 Results are consistent with evidence and depend on controls. Results are consistent with assumptions."},
        {"paper_id": "p2", "paper_name": "Second", "domain": {"code": "x"}, "abstract": "We provide insight into the problem."},
        {"paper_id": "p0", "paper_name": "Empty", "domain": {"code": "x"}, "abstract": "A technical formula."},
    ]
    labels = [
        {"paper_id": "p1", "expressions": [{"text": "consistent with"}, {"text": "depend on"}]},
        {"paper_id": "p2", "expressions": [{"text": "provide insight into"}]},
        {"paper_id": "p0", "expressions": []},
    ]
    for filename, rows in (("abstract_data.jsonl", papers), ("abstract_labels_v2.jsonl", labels)):
        (tmp_path / filename).write_text("\n".join(json.dumps(row) for row in rows))
    return Dataset(tmp_path)


def question(dataset, *, paper_id="p1", text="consistent with", question_id="q1", occurrence=0):
    abstract = dataset.papers[paper_id]["abstract"]
    start = abstract.find(text)
    for _ in range(occurrence):
        start = abstract.find(text, start + 1)
    end = start + len(text)
    last = text.rsplit(" ", 1)[-1]
    answer = last
    others = [word for word in ("of", "at", "to", "from") if word != answer][:3]
    return {
        "question_id": question_id,
        "expression_text": text,
        "expression_span": {"start": start, "end": end},
        "context_span": {"start": 0, "end": len(abstract)},
        "blank_span": {"start": end - len(last), "end": end},
        "options": [{"id": id_, "text": value} for id_, value in zip("ABCD", [answer, *others])],
        "answer_id": "A",
    }


def resolve(item, q=1.0, accepted=None, gates=True):
    item["quality"].update({
        "status": "complete", "source": "human", "q": q,
        "accepted_option_ids": ["A"] if accepted is None else accepted,
        "gates": {"context_sufficient": gates, "no_specialist_knowledge": True, "no_answer_leak": True},
    })


def test_formula_missing_paper_denominator_and_unicode_spans(dataset):
    good = question(dataset)
    non_gold = question(dataset, text="Results", question_id="q2")
    report = evaluate(dataset, [{"paper_id": "p1", "questions": [good, non_gold]}], ["p1", "p2", "p0"], "custom")
    assert report["counts"]["gold"] == 3
    assert report["counts"]["predicted"] == 2
    assert report["counts"]["matched"] == 1
    assert report["counts"]["missing_papers"] == 2
    assert report["papers"][0]["items"][0]["context"].startswith("결과 😀 Results are consistent [BLANK]")
    assert report["metrics"]["precision_pct"] == 50
    assert report["metrics"]["recall_pct"] == pytest.approx(100 / 3)
    assert report["metrics"]["f1_pct"] == 40
    assert report["metrics"]["final_score"] is None
    resolve(report["papers"][0]["items"][0], 0.75)
    resolve(report["papers"][0]["items"][1], 0.5)
    recompute(report)
    assert report["metrics"]["quality_all_pct"] == 62.5
    assert report["metrics"]["quality_matched_pct"] == 75
    assert report["metrics"]["final_score"] == 12.5


def test_unmatched_pending_prevents_final_even_matched_quality_complete(dataset):
    report = evaluate(dataset, [{"paper_id": "p1", "questions": [question(dataset), question(dataset, text="Results", question_id="q2")]}], ["p1"], "custom")
    resolve(report["papers"][0]["items"][0])
    recompute(report)
    assert report["metrics"]["quality_matched_pct"] == 100
    assert report["metrics"]["quality_all_pct"] is None
    assert report["metrics"]["final_score"] is None
    report["papers"][0]["items"][1]["quality"].update(status="error", q=None)
    recompute(report)
    assert report["counts"]["quality_error"] == 1
    assert report["metrics"]["final_score"] is None


def test_duplicate_expression_is_false_positive_with_forced_zero_quality(dataset):
    questions = [question(dataset), question(dataset, question_id="q2", occurrence=1)]
    report = evaluate(dataset, [{"paper_id": "p1", "questions": questions}], ["p1"], "custom")
    first, second = report["papers"][0]["items"]
    assert [first["matched"], second["matched"]] == [True, False]
    assert second["quality"]["q"] == 0
    assert "duplicate_expression" in {issue["code"] for issue in second["issues"]}
    resolve(first)
    resolve(second)  # Recompute must not permit overriding an invalid item.
    recompute(report)
    assert report["counts"]["predicted"] == 2
    assert report["metrics"]["precision_pct"] == 50
    assert report["metrics"]["recall_pct"] == 50
    assert report["metrics"]["quality_all_pct"] == 50
    assert report["metrics"]["final_score"] == 25
    assert second["quality"]["source"] == "structural"


def test_malformed_span_still_matches_expression_but_scores_quality_zero(dataset):
    q = question(dataset)
    q["expression_span"]["start"] += 1
    report = evaluate(dataset, [{"paper_id": "p1", "questions": [q]}], ["p1"], "custom")
    item = report["papers"][0]["items"][0]
    assert item["matched"] is True
    assert item["structure_valid"] is False
    assert item["quality"]["q"] == 0
    assert report["metrics"]["precision_pct"] == 100
    assert report["metrics"]["final_score"] == 0


def test_missing_and_explicit_empty_predictions_are_distinct(dataset):
    report = evaluate(dataset, [], ["p1", "p0"], "custom")
    assert report["metrics"]["final_score"] == 0
    assert report["metrics"]["coverage_pct"] == 0
    assert report["metrics"]["empty_gold_abstention_pct"] == 0
    report = evaluate(dataset, [{"paper_id": "p0", "questions": []}], ["p1", "p0"], "custom")
    assert report["metrics"]["coverage_pct"] == 50
    assert report["metrics"]["empty_gold_abstention_pct"] == 100
    assert report["metrics"]["quality_all_pct"] is None


def test_empty_gold_scope_has_no_final_or_recall(dataset):
    report = evaluate(dataset, [{"paper_id": "p0", "questions": []}], ["p0"], "custom")
    assert report["metrics"]["recall_pct"] is None
    assert report["metrics"]["f1_pct"] is None
    assert report["metrics"]["final_score"] is None
    assert report["metrics"]["empty_gold_abstention_pct"] == 100


@pytest.mark.parametrize("change,code", [
    (lambda q: q.update(blank_span={"start": True, "end": 10}), "invalid_blank_span"),
    (lambda q: q.pop("blank_span"), "invalid_blank_span"),
    (lambda q: q.update(context_span={"start": -1, "end": 10}), "invalid_context_span"),
    (lambda q: q.update(blank_span={"start": 0, "end": 1}), "blank_outside_expression"),
    (lambda q: q["options"][1].update(text=" WITH "), "duplicate_option_text"),
    (lambda q: q["options"][1].update(id="A"), "duplicate_option_id"),
    (lambda q: q.update(answer_id="Z"), "invalid_answer_id"),
    (lambda q: q["options"][0].update(text="wrong"), "answer_not_source"),
    (lambda q: q.update(options=[]), "option_count"),
    (lambda q: q["options"][0].update(unknown="extra"), "invalid_option"),
])
def test_structural_errors_retained_in_denominator(dataset, change, code):
    q = question(dataset)
    change(q)
    report = evaluate(dataset, [{"paper_id": "p1", "questions": [q]}], ["p1"], "custom")
    item = report["papers"][0]["items"][0]
    assert code in {issue["code"] for issue in item["issues"]}
    assert item["quality"]["q"] == 0
    assert report["counts"]["predicted"] == 1


def test_whole_expression_matching_not_blank_and_case_sensitive(dataset):
    q = question(dataset)
    q["expression_text"] = "with"
    q["expression_span"] = deepcopy(q["blank_span"])
    report = evaluate(dataset, [{"paper_id": "p1", "questions": [q]}], ["p1"], "custom")
    assert report["counts"]["matched"] == 0
    assert report["papers"][0]["items"][0]["structure_valid"] is True
    q["expression_text"] = "Consistent with"
    report = evaluate(dataset, [{"paper_id": "p1", "questions": [q]}], ["p1"], "custom")
    assert report["counts"]["matched"] == 0


def test_word_fragment_cannot_be_valid_expression(dataset):
    q = question(dataset, text="sistent")
    report = evaluate(dataset, [{"paper_id": "p1", "questions": [q]}], ["p1"], "custom")
    item = report["papers"][0]["items"][0]
    assert item["matched"] is False
    assert item["structure_valid"] is False
    assert "expression_word_boundary" in {issue["code"] for issue in item["issues"]}


def test_partial_word_blank_is_structurally_invalid(dataset):
    q = question(dataset)
    q["blank_span"]["start"] += 1
    q["options"][0]["text"] = "ith"
    report = evaluate(dataset, [{"paper_id": "p1", "questions": [q]}], ["p1"], "custom")
    item = report["papers"][0]["items"][0]
    assert "blank_word_boundary" in {issue["code"] for issue in item["issues"]}
    assert item["quality"]["q"] == 0


def test_unique_answer_rate_includes_failed_items(dataset):
    q1, q2 = question(dataset), question(dataset, text="depend on", question_id="q2")
    report = evaluate(dataset, [{"paper_id": "p1", "questions": [q1, q2]}], ["p1"], "custom")
    first, second = report["papers"][0]["items"]
    resolve(first)
    resolve(second, q=0, accepted=["A", "B"])
    recompute(report)
    assert report["metrics"]["unique_answer_rate_pct"] == 50
    assert report["metrics"]["quality_all_pct"] == 50


@pytest.mark.parametrize("case", ["unknown", "outside", "duplicate_paper", "duplicate_question", "extra", "too_many", "bad_expression_type", "extra_question_field"])
def test_fatal_contract_failures(dataset, case):
    predictions = [{"paper_id": "p1", "questions": [question(dataset)]}]
    if case == "unknown":
        predictions[0]["paper_id"] = "does-not-exist"
    elif case == "outside":
        predictions[0]["paper_id"] = "p2"
    elif case == "duplicate_paper":
        predictions.append(deepcopy(predictions[0]))
    elif case == "duplicate_question":
        predictions[0]["questions"].append(deepcopy(predictions[0]["questions"][0]))
    elif case == "extra":
        predictions[0]["score"] = 100
    elif case == "too_many":
        predictions[0]["questions"] = [question(dataset, question_id=f"q{i}") for i in range(6)]
    elif case == "bad_expression_type":
        predictions[0]["questions"][0]["expression_text"] = 1
    elif case == "extra_question_field":
        predictions[0]["questions"][0]["quality"] = {"q": 1}
    with pytest.raises(SubmissionError) as exc:
        evaluate(dataset, predictions, ["p1"], "custom")
    assert exc.value.errors


def test_prediction_data_is_not_mutated(dataset):
    predictions = [{"paper_id": "p1", "questions": [question(dataset)]}]
    before = deepcopy(predictions)
    report = evaluate(dataset, predictions, ["p1"], "custom")
    resolve(report["papers"][0]["items"][0])
    assert predictions == before


def mark_temporary(item):
    item["quality"] = {
        "status": "complete", "source": "temporary", "q": 1.0,
        "accepted_option_ids": [], "rubric": None, "gates": None,
        "option_assessments": [], "rationale": "품질 검사를 건너뛴 임시 만점입니다.",
        "reviewer": None, "review_recommended": False,
    }


def test_new_reports_identify_automatic_quality_policy(dataset):
    report = evaluate(dataset, [], ["p1"], "custom")
    assert report["metrics_version"] == "exact-prq-v2"
    assert report["quality_policy_version"] == "auto-conservative-v1"
    assert report["score_mode"] == "reviewed"
    assert report["counts"]["quality_temporary"] == 0
    assert report["counts"]["review_recommended"] == 0


def test_temporary_quality_boosts_only_valid_items_and_keeps_expression_scores(dataset):
    valid = question(dataset)
    invalid = question(dataset, text="depend on", question_id="q2")
    invalid["options"][1]["text"] = invalid["options"][0]["text"]
    unmatched = question(dataset, text="Results", question_id="q3")
    report = evaluate(dataset, [{"paper_id": "p1", "questions": [valid, invalid, unmatched]}], ["p1"], "custom")
    original_expression_scores = {key: report["metrics"][key] for key in ("precision_pct", "recall_pct", "f1_pct")}
    items = report["papers"][0]["items"]
    for item in items:
        mark_temporary(item)
    recompute(report)

    assert {key: report["metrics"][key] for key in original_expression_scores} == original_expression_scores
    assert report["counts"]["predicted"] == 3
    assert report["counts"]["matched"] == 2
    assert report["counts"]["quality_complete"] == 3
    assert report["counts"]["quality_temporary"] == 2
    assert report["counts"]["review_recommended"] == 0
    assert report["score_mode"] == "temporary"
    assert items[1]["quality"]["source"] == "structural"
    assert items[1]["quality"]["q"] == 0
    assert report["metrics"]["quality_all_pct"] == pytest.approx(200 / 3)
    assert report["metrics"]["quality_matched_pct"] == 50
    assert report["metrics"]["final_score"] == pytest.approx(100 / 3)
    assert report["metrics"]["unique_answer_rate_pct"] == 0

    # Replacing the two provisional scores removes the temporary report flag.
    resolve(items[0])
    resolve(items[2])
    recompute(report)
    assert report["score_mode"] == "reviewed"
    assert report["counts"]["quality_temporary"] == 0
    assert report["metrics"]["unique_answer_rate_pct"] == pytest.approx(200 / 3)


def test_temporary_source_cannot_invent_unique_answer_evidence(dataset):
    report = evaluate(dataset, [{"paper_id": "p1", "questions": [question(dataset)]}], ["p1"], "custom")
    item = report["papers"][0]["items"][0]
    mark_temporary(item)
    # Even stale accepted-option fields must not certify a provisional score.
    item["quality"].update(accepted_option_ids=["A"], gates={"context_sufficient": True, "no_specialist_knowledge": True, "no_answer_leak": True})
    recompute(report)
    assert report["metrics"]["quality_matched_pct"] == 100
    assert report["metrics"]["final_score"] == 50
    assert report["metrics"]["unique_answer_rate_pct"] == 0


def test_automatic_uncertainty_finishes_at_zero_with_optional_review(dataset):
    report = evaluate(dataset, [{"paper_id": "p1", "questions": [question(dataset)]}], ["p1"], "custom")
    item = report["papers"][0]["items"][0]
    resolve(item, q=0)
    item["quality"].update(
        source="llm", review_recommended=True,
        option_assessments=[
            {"id": "A", "verdict": "acceptable", "reason": "정답은 자연스럽습니다."},
            {"id": "B", "verdict": "uncertain", "reason": "추가로 허용 가능한지 불확실합니다."},
            {"id": "C", "verdict": "incorrect", "reason": "부적절합니다."},
            {"id": "D", "verdict": "incorrect", "reason": "부적절합니다."},
        ],
    )
    recompute(report)
    assert report["counts"]["quality_complete"] == 1
    assert report["counts"]["quality_pending"] == 0
    assert report["counts"]["needs_review"] == 0
    assert report["counts"]["review_recommended"] == 1
    assert report["metrics"]["final_score"] == 0
    assert report["metrics"]["quality_all_pct"] == 0
    assert report["metrics"]["unique_answer_rate_pct"] == 0
    assert report["score_mode"] == "reviewed"


def test_structural_failure_cannot_be_changed_to_temporary_or_recommended(dataset):
    q = question(dataset)
    q["answer_id"] = "Z"
    report = evaluate(dataset, [{"paper_id": "p1", "questions": [q]}], ["p1"], "custom")
    item = report["papers"][0]["items"][0]
    mark_temporary(item)
    item["quality"]["review_recommended"] = True
    recompute(report)
    assert item["quality"]["source"] == "structural"
    assert item["quality"]["q"] == 0
    assert report["counts"]["quality_temporary"] == 0
    assert report["counts"]["review_recommended"] == 0
    assert report["score_mode"] == "reviewed"
    assert report["metrics"]["final_score"] == 0


def test_recomputing_legacy_report_does_not_relabel_its_policy(dataset):
    report = evaluate(dataset, [{"paper_id": "p1", "questions": [question(dataset)]}], ["p1"], "custom")
    report["metrics_version"] = "exact-prq-v1"
    report.pop("quality_policy_version")
    resolve(report["papers"][0]["items"][0])
    recompute(report)
    assert report["metrics_version"] == "exact-prq-v1"
    assert "quality_policy_version" not in report
    assert report["metrics"]["final_score"] == 50
