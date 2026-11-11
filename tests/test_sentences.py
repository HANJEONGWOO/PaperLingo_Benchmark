import pytest

from paperlingo.sentences import abstract_sentences, sentence_count


@pytest.mark.parametrize("text, expected", [
    ("The first result holds. The second result follows! Does the third hold? We prove it. This ends the proof.", 5),
    ("A single sentence\nwrapped across\nfive lines\nwith no extra\nterminal punctuation.", 1),
    ("We use e.g. a 3.14 threshold. See Fig. 2 for results. Dr. A. Smith agrees. The U.S. data agree. We conclude this holds", 5),
    (r"Our result follows Ziglin ({\em Funktsional. Anal. i Prilozhen.}, 17:8--23, 1983). We extend the proof. The result holds.", 3),
    (r"We use [Bilge A.H., \c{S}. Ko\c{c}ak, Linear Algebra 419 (2006) 417-439. 3]. Our proof differs.", 2),
    ("Consider a vector space V. We prove the result.", 2),
    ("This concerns Nature of Science (NoS). We study its impact.", 2),
    (r"We use $x. y$ and \(z. w\) as formal notation. Our argument remains valid.", 2),
    (r"The displayed formula is $$a. b$$. We use \[c. d\] next.", 2),
    ("Introduction. We study an equation. 1. We derive a solution.", 2),
    ("We follow a formula (see the discussion. It has several parts). The proof follows.", 2),
])
def test_scientific_sentence_boundaries(text, expected):
    assert sentence_count(text) == expected
    assert all(sentence in text for sentence in abstract_sentences(text))


def test_scientific_citations_do_not_make_short_abstracts_eligible():
    from pathlib import Path
    from paperlingo.dataset import Dataset

    dataset = Dataset(Path(__file__).resolve().parents[1] / "data")
    assert dataset.sentence_counts["math/0404367"] == 3
    assert "math/0404367" not in dataset.splits["evaluation"]
    assert dataset.sentence_counts["1101.3961"] == 5
    assert "1101.3961" in dataset.splits["evaluation"]
