from copy import deepcopy
import json
from pathlib import Path

import pytest

from paperlingo.dataset import Dataset, occurrences
from paperlingo.scoring import evaluate
from paperlingo.training_answers import generate_training_answers, training_answer_metadata


DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def test_all_training_references_have_structurally_valid_unreviewed_answers():
    dataset = Dataset(DATA_DIR)
    papers_before, labels_before = deepcopy(dataset.papers), deepcopy(dataset.labels)
    generated = generate_training_answers(dataset)
    assert [paper["paper_id"] for paper in generated] == dataset.splits["train"]
    assert len(generated) == 1043
    assert not {paper["paper_id"] for paper in generated} & set(dataset.splits["evaluation"])
    indices = set()
    for paper in generated:
        references = dataset.labels[paper["paper_id"]]
        assert len(paper["questions"]) == len(references) <= 5
        assert [question["expression_text"] for question in paper["questions"]] == [item["text"] for item in references]
        for question in paper["questions"]:
            assert set(question) == {"expression_text", "options", "answer_id"}
            assert len(question["options"]) == 4
            assert len({option.casefold() for option in question["options"]}) == 4
            assert type(question["answer_id"]) is int
            indices.add(question["answer_id"])
            assert occurrences(question["expression_text"], question["options"][question["answer_id"]], whole_words=True)
    assert indices == {0, 1, 2, 3}
    assert any(not paper["questions"] for paper in generated)
    report = evaluate(dataset, generated, dataset.splits["train"], "train")
    assert report["counts"]["gold"] == report["counts"]["matched"] == report["counts"]["predicted"] == 4324
    assert report["counts"]["quality_pending"] == 4324
    assert report["counts"]["quality_complete"] == report["counts"]["quality_temporary"] == 0
    for metric in ("precision_pct", "recall_pct", "f1_pct", "structural_validity_pct", "coverage_pct", "empty_gold_abstention_pct"):
        assert report["metrics"][metric] == 100
    assert report["metrics"]["final_score"] is None
    assert report["metrics"]["quality_matched_pct"] is None
    assert dataset.papers == papers_before
    assert dataset.labels == labels_before


def test_generation_and_metadata_are_reproducible_independent_copies():
    dataset = Dataset(DATA_DIR)
    original = generate_training_answers(dataset)
    changed = generate_training_answers(dataset)
    assert changed == original
    changed[0]["questions"][0]["options"][0] = "tampered"
    changed[0]["questions"].clear()
    assert generate_training_answers(dataset) == original
    metadata = training_answer_metadata()
    assert metadata["version"] == "rule-based-v1"
    assert metadata["quality_status"] == "unreviewed"
    assert "미검수" in metadata["label"]
    assert metadata["limitations"]
    metadata["limitations"].clear()
    metadata["quality_status"] = "approved"
    assert training_answer_metadata()["quality_status"] == "unreviewed"
    assert training_answer_metadata()["limitations"]


@pytest.mark.parametrize("text,expression_type,answer,choices", [
    ("consistent with", "preposition_pattern", "with", {"with", "to", "for", "on"}),
    ("We show that", "rhetorical_frame", "that", {"that", "what", "which", "whom"}),
    ("larger than", "grammar_pattern", "than", {"than", "then", "that", "when"}),
    ("allows us to prove", "verb_pattern", "prove", {"prove", "proves", "proved", "proven"}),
    ("exhibit", "academic_vocabulary", "exhibit", {"exhibit", "exhibits", "exhibited", "exhibiting"}),
    ("understand", "academic_vocabulary", "understand", {"understand", "understands", "understood", "understanding"}),
    ("overcome", "academic_vocabulary", "overcome", {"overcome", "overcomes", "overcame", "overcoming"}),
    ("underpin", "academic_vocabulary", "underpin", {"underpin", "underpins", "underpinned", "underpinning"}),
    ("may have been", "grammar_pattern", "been", {"been", "be", "is", "are"}),
    ("Furthermore", "discourse_marker", "Furthermore", {"Furthermore", "However", "Therefore", "Instead"}),
    ("off-the-shelf", "academic_vocabulary", "off-the-shelf", {"off-the-shelf", "custom-built", "unavailable", "experimental"}),
    ("state-of-the-art", "academic_vocabulary", "state-of-the-art", {"state-of-the-art", "obsolete", "conventional", "experimental"}),
    ("unprecedented", "academic_vocabulary", "unprecedented", {"unprecedented", "established", "conventional", "predictable"}),
    ("a key requirement", "collocation", "requirement", {"requirement", "assumption", "constraint", "implication"}),
])
def test_rules_choose_source_answers_and_relevant_types(tmp_path, text, expression_type, answer, choices):
    paper = {"paper_id": "unicode-논문", "abstract": f"앞 문장. {text} applies here."}
    label = {"paper_id": paper["paper_id"], "expressions": [{"text": text, "expression_type": expression_type}]}
    (tmp_path / "abstract_data.jsonl").write_text(json.dumps(paper), encoding="utf-8")
    (tmp_path / "abstract_labels_v2.jsonl").write_text(json.dumps(label), encoding="utf-8")
    dataset = Dataset(tmp_path)
    generated = generate_training_answers(dataset)
    question = generated[0]["questions"][0]
    assert question["expression_text"] == text
    assert question["options"][question["answer_id"]] == answer
    assert set(question["options"]) == choices
    assert evaluate(dataset, generated, dataset.splits["train"], "train")["metrics"]["structural_validity_pct"] == 100
