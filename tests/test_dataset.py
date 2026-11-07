import hashlib
import json
from pathlib import Path

import pytest

from paperlingo.dataset import Dataset, DatasetError


DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def test_real_reference_integrity_and_splits():
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in DATA_DIR.glob("*.jsonl")}
    dataset = Dataset(DATA_DIR)
    summary = dataset.summary()
    assert summary["paper_count"] == 1490
    assert summary["expression_count"] == 6188
    assert summary["domain_count"] == 149
    assert summary["zero_gold_count"] == 12
    assert summary["splits"]["development"] == 1043
    assert summary["splits"]["validation"] == 149
    assert summary["splits"]["test"] == 298
    assert len(dataset.version) == 64
    assert summary["audit"]["word_boundary_flag_count"] == 0
    assert summary["audit"]["duplicate_abstract_groups"] == []
    partitions = [set(dataset.splits[name]) for name in ("development", "validation", "test")]
    assert set.union(*partitions) == set(dataset.papers)
    assert not (partitions[0] & partitions[1] or partitions[0] & partitions[2] or partitions[1] & partitions[2])
    for domain in {paper["domain"]["code"] for paper in dataset.papers.values()}:
        counts = [sum(dataset.papers[id_]["domain"]["code"] == domain for id_ in part) for part in partitions]
        assert counts == [7, 1, 2]
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


def test_dataset_version_changes_with_label_edits(tmp_path):
    papers = [{"paper_id": "p", "abstract": "A valuable result."}]
    labels = [{"paper_id": "p", "expressions": [{"text": "valuable"}]}]
    write_fixture(tmp_path, papers, labels)
    original = Dataset(tmp_path).version
    labels[0]["expressions"][0]["text"] = "result"
    write_fixture(tmp_path, papers, labels)
    assert Dataset(tmp_path).version != original


def test_search_returns_summary_not_entire_dataset():
    dataset = Dataset(DATA_DIR)
    result = dataset.search("0911.3369")
    assert result["total"] == 1
    assert result["items"][0]["paper_id"] == "0911.3369"
    assert "abstract" not in result["items"][0]
