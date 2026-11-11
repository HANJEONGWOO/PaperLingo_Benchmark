import hashlib
import json
from pathlib import Path

import pytest

from paperlingo.dataset import Dataset, DatasetError
from paperlingo.judge import temporary_quality
from paperlingo.scoring import evaluate, recompute


DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def test_real_reference_integrity_and_splits():
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in DATA_DIR.glob("*.jsonl")}
    dataset = Dataset(DATA_DIR)
    summary = dataset.summary()
    assert summary["paper_count"] == 1490
    assert summary["expression_count"] == 6853
    assert summary["domain_count"] == 149
    assert summary["zero_gold_count"] == 23
    assert summary["splits"] == {"demo": 3, "evaluation": 1044}
    assert summary["split_stats"] == {
        "demo": {"paper_count": 3, "expression_count": 9, "domain_count": 2},
        "evaluation": {"paper_count": 1044, "expression_count": 5497, "domain_count": 147},
    }
    assert summary["split_version"] == Dataset.SPLIT_VERSION == "evaluation-min5sentences-v3"
    assert len(dataset.version) == 64
    assert summary["audit"]["word_boundary_flag_count"] == 0
    assert summary["audit"]["duplicate_abstract_groups"] == []
    partitions = [set(dataset.splits[name]) for name in ("demo", "evaluation")]
    excluded = set(dataset.papers) - set.union(*partitions)
    assert len(excluded) == 443
    assert all(dataset.sentence_counts[pid] < 5 for pid in excluded)
    assert partitions[1] == {pid for pid in dataset.papers if pid not in partitions[0] and dataset.sentence_counts[pid] >= 5}
    assert not partitions[0] & partitions[1]
    assert [len(partition) for partition in partitions] == [3, 1044]
    assert dataset.splits["demo"] == ["0911.3369", "1109.3182", "1709.01217"]
    assert set(dataset.splits["demo"]) <= partitions[0]
    assert not set(dataset.splits["demo"]) & partitions[1]
    assert sum(len(dataset.labels[pid]) for pid in dataset.splits["evaluation"]) == 5497
    assert summary["evaluation_filter"] == {
        "minimum_sentences": 5, "sentence_policy_version": "abstract-sentences-v1",
        "source_paper_count": 1487, "eligible_paper_count": 1044, "excluded_paper_count": 443,
        "sampling": "none; retain every eligible paper",
    }
    assert dataset.splits == Dataset(DATA_DIR).splits
    assert dataset.version == Dataset(DATA_DIR).version
    assert before == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in DATA_DIR.glob("*.jsonl")}


def test_demo_has_empty_gold_and_explicit_authored_samples():
    dataset = Dataset(DATA_DIR)
    demo = dataset.demo_predictions()
    assert len(demo) == 3
    assert [row["paper_id"] for row in demo] == dataset.splits["demo"]
    assert any(not row["questions"] and not dataset.labels[row["paper_id"]] for row in demo)
    assert any(row["questions"] for row in demo)
    assert all("직접 작성" in q["learning_point"] for row in demo for q in row["questions"])
    demo[0]["questions"][0]["expression_text"] = "tampered"
    assert dataset.demo_predictions()[0]["questions"][0]["expression_text"] != "tampered"


@pytest.mark.parametrize("compact", [False, True])
def test_demo_covers_all_nine_gold_expressions_without_duplicates(compact):
    dataset = Dataset(DATA_DIR)
    predictions = dataset.demo_predictions(compact=compact)
    assert [row["paper_id"] for row in predictions] == ["0911.3369", "1109.3182", "1709.01217"]
    assert [len(row["questions"]) for row in predictions] == [4, 5, 0]
    for row in predictions:
        texts = [question["expression_text"] for question in row["questions"]]
        assert len(texts) == len(set(texts)) <= 5
        assert set(texts) == {expression["text"] for expression in dataset.labels[row["paper_id"]]}
    report = evaluate(dataset, predictions, dataset.splits["demo"], "demo")
    assert report["counts"]["gold"] == report["counts"]["predicted"] == report["counts"]["matched"] == 9
    assert report["counts"]["quality_pending"] == 9
    for metric in ("precision_pct", "recall_pct", "f1_pct", "structural_validity_pct", "coverage_pct", "empty_gold_abstention_pct"):
        assert report["metrics"][metric] == 100
    assert report["metrics"]["final_score"] is None
    assert all(not item["issues"] for paper in report["papers"] for item in paper["items"])
    assert all(not paper["missing_expressions"] for paper in report["papers"])
    # Temporary full credit demonstrates the complete scoring path; it does not
    # claim that the authored choices have received real quality validation.
    for paper in report["papers"]:
        for item in paper["items"]:
            item["quality"] = temporary_quality(item)
    recompute(report)
    assert report["metrics"]["final_score"] == 100
    assert report["metrics"]["quality_matched_pct"] == 100
    assert report["counts"]["quality_temporary"] == 9
    assert report["score_mode"] == "temporary"
    assert report["metrics"]["unique_answer_rate_pct"] == 0


def test_demo_keeps_preferred_first_questions_and_stable_legacy_identifiers():
    dataset = Dataset(DATA_DIR)
    first, second, empty = dataset.demo_predictions()
    assert first["questions"][0]["expression_text"] == "identical to"
    assert second["questions"][0]["expression_text"] == "consistent with"
    assert first["questions"][0]["question_id"] == "demo-0911.3369-valid"
    assert second["questions"][0]["question_id"] == "demo-1109.3182-valid"
    for row in (first, second):
        assert [question["question_id"] for question in row["questions"][1:]] == [
            f"demo-{row['paper_id']}-expression-{position}" for position in range(2, len(row["questions"]) + 1)
        ]
    assert empty["questions"] == []


def test_downloadable_compact_file_matches_live_demo_and_returns_fresh_copies():
    dataset = Dataset(DATA_DIR)
    path = DATA_DIR.parent / "examples" / "predictions.jsonl"
    saved = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert saved == dataset.demo_predictions(compact=True)
    altered = dataset.demo_predictions(compact=True)
    altered[0]["questions"][0]["options"][0] = "tampered"
    altered[1]["questions"].clear()
    assert dataset.demo_predictions(compact=True) == saved


def write_fixture(tmp_path, papers, labels):
    for filename, rows in (("abstract_data.jsonl", papers), ("abstract_labels_v2.jsonl", labels)):
        (tmp_path / filename).write_text("\n".join(json.dumps(row) for row in rows))


@pytest.mark.parametrize("case", ["duplicate", "missing", "extra", "absent_text", "duplicate_label"])
def test_bad_reference_fails_loudly(tmp_path, case):
    papers = [{"paper_id": "p", "abstract": "A valuable result."}]
    labels = [{"paper_id": "p", "expressions": [{"text": "valuable"}]}]
    if case == "duplicate":
        papers *= 2
    elif case == "missing":
        labels = []
    elif case == "extra":
        labels.append({"paper_id": "extra", "expressions": []})
    elif case == "absent_text":
        labels[0]["expressions"][0]["text"] = "invented"
    elif case == "duplicate_label":
        labels[0]["expressions"] *= 2
    write_fixture(tmp_path, papers, labels)
    with pytest.raises(DatasetError):
        Dataset(tmp_path)


@pytest.mark.parametrize("count, ok", [(10, True), (11, False)])
def test_reference_expression_limit_is_ten(tmp_path, count, ok):
    words = [f"w{index}" for index in range(11)]
    papers = [{"paper_id": "p", "abstract": " ".join(words) + "."}]
    labels = [{"paper_id": "p", "expressions": [{"text": word} for word in words[:count]]}]
    write_fixture(tmp_path, papers, labels)
    if ok:
        assert len(Dataset(tmp_path).labels["p"]) == 10
    else:
        with pytest.raises(DatasetError):
            Dataset(tmp_path)


def test_dataset_version_changes_with_label_edits(tmp_path):
    papers = [{"paper_id": "p", "abstract": "A valuable result."}]
    labels = [{"paper_id": "p", "expressions": [{"text": "valuable"}]}]
    write_fixture(tmp_path, papers, labels)
    original = Dataset(tmp_path).version
    labels[0]["expressions"][0]["text"] = "result"
    write_fixture(tmp_path, papers, labels)
    assert Dataset(tmp_path).version != original


def test_split_configuration_does_not_change_source_dataset_version(monkeypatch):
    # The grammar answer key is tied to the current split, so compare without it.
    monkeypatch.setattr(Dataset, "EVALUATION_LABELS_FILE", "absent-evaluation-labels.jsonl")
    original = Dataset(DATA_DIR)
    monkeypatch.setattr(Dataset, "EXAMPLE_PAPER_IDS", ("2208.09462", "1109.3182", "1709.01217"))
    monkeypatch.setattr(Dataset, "SPLIT_VERSION", "test-alternate-policy")
    alternate = Dataset(DATA_DIR)
    assert alternate.splits != original.splits
    assert alternate.version == original.version
    assert alternate.summary()["split_version"] == "test-alternate-policy"
    assert not set(alternate.splits["demo"]) & set(alternate.splits["evaluation"])


def test_splits_ignore_source_row_order_and_preserve_references(tmp_path):
    papers = [
        {"paper_id": f"{domain}-{index}", "domain": domain, "abstract": f"A valuable result {index}."}
        for domain in ("first", "second") for index in range(10)
    ]
    labels = [
        {"paper_id": paper["paper_id"], "expressions": [{"text": "valuable"}]}
        for paper in papers
    ]
    write_fixture(tmp_path, papers, labels)
    original = Dataset(tmp_path)
    assert original.papers == {paper["paper_id"]: paper for paper in papers}
    assert original.labels == {row["paper_id"]: row["expressions"] for row in labels}
    write_fixture(tmp_path, list(reversed(papers)), list(reversed(labels)))
    reordered = Dataset(tmp_path)
    assert reordered.splits == original.splits
    assert len(reordered.splits["demo"]) == 3
    assert reordered.splits["evaluation"] == []  # Every synthetic abstract has one sentence.
    assert reordered.evaluation_filter["excluded_paper_count"] == 17


def test_tiny_dataset_keeps_public_examples_out_of_evaluation(tmp_path):
    papers = [{"paper_id": "p", "abstract": "A valuable result."}]
    labels = [{"paper_id": "p", "expressions": [{"text": "valuable"}]}]
    write_fixture(tmp_path, papers, labels)
    dataset = Dataset(tmp_path)
    assert dataset.splits == {"demo": ["p"], "evaluation": []}


def test_real_evaluation_labels_come_from_grammar_answer_key():
    dataset = Dataset(DATA_DIR)
    rows = [json.loads(line) for line in (DATA_DIR / Dataset.EVALUATION_LABELS_FILE).read_text().splitlines() if line.strip()]
    assert [row["paper_id"] for row in rows] == dataset.splits["evaluation"]
    assert all(dataset.labels[row["paper_id"]] == row["expressions"] for row in rows)
    assert dataset.summary()["label_sources"] == {
        "default": "abstract_labels_v2.jsonl", "evaluation": Dataset.EVALUATION_LABELS_FILE,
    }


FIVE_SENTENCES = ("We report a valuable result. The method is simple. The data are public. "
                  "The code is released. The results are robust.")


def write_override_fixture(tmp_path, override):
    papers = [{"paper_id": pid, "abstract": FIVE_SENTENCES} for pid in ("d1", "d2", "d3", "e1", "e2")]
    labels = [{"paper_id": paper["paper_id"], "expressions": [{"text": "valuable"}]} for paper in papers]
    write_fixture(tmp_path, papers, labels)
    (tmp_path / Dataset.EVALUATION_LABELS_FILE).write_text("\n".join(json.dumps(row) for row in override))


def test_evaluation_label_file_replaces_only_evaluation_labels(tmp_path):
    write_override_fixture(tmp_path, [])
    (tmp_path / Dataset.EVALUATION_LABELS_FILE).unlink()
    without = Dataset(tmp_path)
    evaluation = without.splits["evaluation"]
    assert len(evaluation) == 2
    override = [{"paper_id": evaluation[0], "expressions": [{"text": "result"}]},
                {"paper_id": evaluation[1], "expressions": []}]
    write_override_fixture(tmp_path, override)
    dataset = Dataset(tmp_path)
    assert dataset.splits == without.splits
    assert dataset.version != without.version
    assert dataset.labels[evaluation[0]] == [{"text": "result"}]
    assert dataset.labels[evaluation[1]] == []
    assert all(dataset.labels[pid] == [{"text": "valuable"}] for pid in dataset.splits["demo"])


@pytest.mark.parametrize("case", ["missing", "demo", "unknown", "absent_text"])
def test_bad_evaluation_label_file_fails_loudly(tmp_path, case):
    write_override_fixture(tmp_path, [])
    (tmp_path / Dataset.EVALUATION_LABELS_FILE).unlink()
    splits = Dataset(tmp_path).splits
    override = [{"paper_id": pid, "expressions": [{"text": "valuable"}]} for pid in splits["evaluation"]]
    if case == "missing":
        override.pop()
    elif case == "demo":
        override.append({"paper_id": splits["demo"][0], "expressions": []})
    elif case == "unknown":
        override.append({"paper_id": "unknown", "expressions": []})
    elif case == "absent_text":
        override[0]["expressions"][0]["text"] = "invented"
    write_override_fixture(tmp_path, override)
    with pytest.raises(DatasetError):
        Dataset(tmp_path)


def test_search_returns_summary_not_entire_dataset():
    dataset = Dataset(DATA_DIR)
    result = dataset.search("0911.3369")
    assert result["total"] == 1
    assert result["items"][0]["paper_id"] == "0911.3369"
    assert "abstract" not in result["items"][0]
