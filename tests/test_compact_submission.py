"""Regression checks for the three-field public question contract."""

from copy import deepcopy
import json

import pytest

from paperlingo.dataset import Dataset
from paperlingo.scoring import SubmissionError, evaluate, recompute
from paperlingo.submission import generated_question_id, normalize_question


def dataset_for(tmp_path, abstract, expressions=None):
    if expressions is None:
        expressions = ["consistent with"]
    papers = [
        {"paper_id": "p/1", "paper_name": "One", "domain": {"code": "d"}, "abstract": abstract},
        {"paper_id": "p/2", "paper_name": "Two", "domain": {"code": "d"}, "abstract": abstract},
    ]
    labels = [{"paper_id": paper["paper_id"], "expressions": [{"text": text} for text in expressions]} for paper in papers]
    for name, rows in (("abstract_data.jsonl", papers), ("abstract_labels_v2.jsonl", labels)):
        (tmp_path / name).write_text("\n".join(json.dumps(row) for row in rows))
    return Dataset(tmp_path)


@pytest.fixture
def dataset(tmp_path):
    return dataset_for(tmp_path, "Previous sentence. 😀 Results, e.g. estimates at 3.14 units, are consistent with evidence. Next sentence.")


def compact(**updates):
    value = {"expression_text": "consistent with", "options": ["with", "of", "to", "from"], "answer_id": 0}
    value.update(updates)
    return value


def score(dataset, questions):
    return evaluate(dataset, [{"paper_id": "p/1", "questions": questions}], ["p/1"], "custom")


def first_item(report):
    return report["papers"][0]["items"][0]


def test_compact_normalization_preserves_internal_contract_and_unicode(dataset):
    raw = compact()
    original = deepcopy(raw)
    report = score(dataset, [raw])
    item = first_item(report)
    assert raw == original
    assert item["structure_valid"] is True
    assert item["matched"] is True
    assert item["context"] == "😀 Results, e.g. estimates at 3.14 units, are consistent [BLANK] evidence."
    assert item["options"] == [{"id": key, "text": value} for key, value in zip("ABCD", original["options"])]
    assert item["answer_id"] == "A"
    assert item["submitted_question"] == original
    assert item["input_format"] == "compact-v1"
    assert report["submission_formats"] == ["compact-v1"]
    assert report["inference_policy"]["version"] == "compact-first-sentence-v1"
    assert report["metrics_version"] == "exact-rq-v3"
    assert report["metrics"]["precision_pct"] == 100
    assert report["metrics"]["recall_pct"] == 100
    assert report["metrics"]["final_score"] is None
    abstract = dataset.papers["p/1"]["abstract"]
    blank = item["blank_span"]
    context = item["context_span"]
    assert abstract[blank["start"]:blank["end"]] == "with"
    assert item["context"].replace("[BLANK]", "with") == abstract[context["start"]:context["end"]]


@pytest.mark.parametrize("index", range(4))
def test_each_zero_based_answer_index_maps_to_correct_internal_option(dataset, index):
    choices = ["of", "to", "from"]
    choices.insert(index, "with")
    item = first_item(score(dataset, [compact(options=choices, answer_id=index)]))
    assert item["answer_id"] == "ABCD"[index]
    assert item["structure_valid"] is True
    assert item["context"].count("[BLANK]") == 1


@pytest.mark.parametrize("index", [True, False, -1, 4, 0.0, 1.5, "0", "A", None, [], {}])
def test_bad_answer_index_is_scored_zero_without_dropping_expression(dataset, index):
    report = score(dataset, [compact(answer_id=index)])
    item = first_item(report)
    assert report["counts"]["predicted"] == 1
    assert report["counts"]["matched"] == 1
    assert item["structure_valid"] is False
    assert item["quality"]["q"] == 0
    assert item["submitted_question"]["answer_id"] == index
    assert "invalid_answer_index" in {issue["code"] for issue in item["issues"]}
    assert report["metrics"]["final_score"] == 0


@pytest.mark.parametrize("options", [
    [], ["with"], ["with", "of", "to", "from", "for"],
    ["with", "of", "to", 5], ["with", None, "to", "from"],
    ["with", {"id": "B", "text": "of"}, "to", "from"],
    ["with", " WITH ", "to", "from"], ["with", "", "to", "from"],
    {"A": "with"}, None, "with",
])
def test_malformed_compact_options_remain_scoreable(dataset, options):
    report = score(dataset, [compact(options=options)])
    item = first_item(report)
    assert report["counts"]["predicted"] == 1
    assert item["matched"] is True
    assert item["structure_valid"] is False
    assert item["quality"]["q"] == 0
    assert item["submitted_question"]["options"] == options


@pytest.mark.parametrize("answer", ["for", "With", "ith"])
def test_answer_must_appear_as_exact_whole_word_inside_expression(dataset, answer):
    item = first_item(score(dataset, [compact(options=[answer, "of", "to", "from"])]))
    assert item["matched"] is True
    assert item["quality"]["q"] == 0
    assert "answer_not_in_expression" in {issue["code"] for issue in item["issues"]}


def test_answer_in_abstract_but_outside_expression_does_not_define_blank(dataset):
    item = first_item(score(dataset, [compact(options=["evidence", "of", "to", "from"])]))
    assert item["blank_span"] is None
    assert item["quality"]["q"] == 0


def test_first_whole_word_occurrence_selected_for_expression_and_answer(tmp_path):
    abstract = "The outcome is inconsistent with noise. We are consistent with evidence. Again consistent with observations."
    dataset = dataset_for(tmp_path, abstract)
    item = first_item(score(dataset, [compact()]))
    assert item["expression_span"]["start"] == abstract.index("consistent with", abstract.index("We are"))
    assert item["context"] == "We are consistent [BLANK] evidence."
    assert item["inference"]["expression_occurrence_count"] == 2
    assert item["inference"]["expression_occurrence"] == 0
    abstract = "We refer to compare to prior work."
    dataset = dataset_for(tmp_path, abstract, ["to compare to"])
    item = first_item(score(dataset, [compact(expression_text="to compare to", options=["to", "of", "at", "from"])]))
    assert item["blank_span"]["start"] == abstract.index("to")
    assert item["inference"]["answer_occurrence_count"] == 2
    assert item["inference"]["answer_occurrence"] == 0


def test_answer_substring_within_another_word_is_skipped(tmp_path):
    abstract = "We condition on observations."
    dataset = dataset_for(tmp_path, abstract, ["condition on"])
    item = first_item(score(dataset, [compact(expression_text="condition on", options=["on", "of", "to", "from"])]))
    assert item["context"] == "We condition [BLANK] observations."
    assert item["blank_span"]["start"] == abstract.index(" on ") + 1
    assert item["inference"]["answer_occurrence_count"] == 1


@pytest.mark.parametrize("abstract,expected", [
    ("Question? Previous! Results are consistent with evidence. Next!", "Results are consistent [BLANK] evidence."),
    ("Previous line\nResults are consistent with evidence\nNext line", "Results are consistent [BLANK] evidence"),
    ("Previous line\rResults are consistent with evidence\rNext line", "Results are consistent [BLANK] evidence"),
    ("Previous. Dr. A. Smith reports consistent with evidence. Next.", "Dr. A. Smith reports consistent [BLANK] evidence."),
    ("Previous. Results, i.e. estimates from the U.S. cohort, are consistent with evidence. Next.", "Results, i.e. estimates from the U.S. cohort, are consistent [BLANK] evidence."),
    ("Previous. Results in Fig. 2 are consistent with evidence. Next.", "Results in Fig. 2 are consistent [BLANK] evidence."),
    ('Previous. "Results are consistent with evidence." Next.', '"Results are consistent [BLANK] evidence."'),
    ("Previous?! Results are consistent with evidence! Next.", "Results are consistent [BLANK] evidence!"),
])
def test_sentence_context_boundaries(tmp_path, abstract, expected):
    dataset = dataset_for(tmp_path, abstract)
    item = first_item(score(dataset, [compact()]))
    assert item["context"] == expected
    assert item["structure_valid"] is True


def test_expression_crossing_sentence_boundary_is_never_cut(tmp_path):
    abstract = "Earlier. We have evidence. Results are consistent with prior work. Later."
    expression = "evidence. Results are consistent with"
    dataset = dataset_for(tmp_path, abstract, [expression])
    item = first_item(score(dataset, [compact(expression_text=expression)]))
    assert item["context"] == "We have evidence. Results are consistent [BLANK] prior work."
    assert item["context_span"]["start"] <= item["expression_span"]["start"]
    assert item["context_span"]["end"] >= item["expression_span"]["end"]
    assert item["structure_valid"] is True


def test_expression_not_in_source_stays_in_denominator(dataset):
    report = score(dataset, [compact(expression_text="never in source")])
    item = first_item(report)
    assert report["counts"]["predicted"] == 1
    assert report["counts"]["matched"] == 0
    assert item["quality"]["q"] == 0
    assert item["expression_span"] is None


def test_duplicate_compact_expression_counts_fp_and_has_unique_generated_id(dataset):
    report = score(dataset, [compact(), compact()])
    first, second = report["papers"][0]["items"]
    assert first["question_id"] != second["question_id"]
    assert report["counts"]["predicted"] == 2
    assert report["counts"]["matched"] == 1
    assert second["matched"] is False
    assert second["quality"]["q"] == 0


def test_question_ids_stable_when_paper_order_changes_and_unique_across_papers(dataset):
    predictions = [{"paper_id": id_, "questions": [compact()]} for id_ in ["p/1", "p/2"]]
    first = evaluate(dataset, predictions, ["p/1", "p/2"], "custom")
    second = evaluate(dataset, predictions[::-1], ["p/2", "p/1"], "custom")
    first_ids = {paper["paper_id"]: paper["items"][0]["question_id"] for paper in first["papers"]}
    second_ids = {paper["paper_id"]: paper["items"][0]["question_id"] for paper in second["papers"]}
    assert first_ids == second_ids
    assert len(set(first_ids.values())) == 2


def test_compact_and_equivalent_legacy_have_identical_scores(dataset):
    generated, _ = normalize_question(compact(), dataset.papers["p/1"]["abstract"], "p/1", 0)
    legacy = {key: value for key, value in generated.items() if key not in {"input_format", "submitted_question", "inference"}}
    compact_report = score(dataset, [compact()])
    legacy_report = score(dataset, [legacy])
    assert compact_report["counts"] == legacy_report["counts"]
    assert compact_report["metrics"] == legacy_report["metrics"]
    assert first_item(legacy_report)["input_format"] == "legacy-v1"
    for report in (compact_report, legacy_report):
        item = first_item(report)
        item["quality"].update(status="complete", source="human", q=0.75, accepted_option_ids=["A"], gates={"context_sufficient": True, "no_specialist_knowledge": True, "no_answer_leak": True})
        recompute(report)
    assert compact_report["metrics"] == legacy_report["metrics"]
    assert compact_report["metrics"]["final_score"] == 75


@pytest.mark.parametrize("field,value", [
    ("question_id", "caller-selected"), ("expression_type", "collocation"),
    ("learning_point", "hint"), ("expression_span", {"start": 0, "end": 1}),
    ("context_span", {"start": 0, "end": 1}), ("blank_span", {"start": 0, "end": 1}),
    ("unknown", "extra"), ("quality", {"q": 1}),
])
def test_compact_extra_fields_and_hybrids_are_fatal(dataset, field, value):
    with pytest.raises(SubmissionError):
        score(dataset, [compact(**{field: value})])


@pytest.mark.parametrize("expression", [None, 123, [], "x" * 1001])
def test_invalid_expression_contract_is_fatal(dataset, expression):
    with pytest.raises(SubmissionError):
        score(dataset, [compact(expression_text=expression)])


def test_generated_id_collision_with_legacy_is_rejected(dataset):
    item, _ = normalize_question(compact(), dataset.papers["p/1"]["abstract"], "p/1", 0)
    legacy = {key: value for key, value in item.items() if key not in {"input_format", "submitted_question", "inference"}}
    legacy["question_id"] = generated_question_id("p/1", 1)
    with pytest.raises(SubmissionError):
        score(dataset, [legacy, compact()])


def test_malformed_unicode_unknown_paper_still_raises_submission_error(dataset):
    with pytest.raises(SubmissionError):
        evaluate(dataset, [{"paper_id": "\ud800", "questions": [compact()]}], ["p/1"], "custom")
