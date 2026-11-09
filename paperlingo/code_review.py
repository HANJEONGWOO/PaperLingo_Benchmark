"""Four deterministic option checks; no linguistic or contextual judgments."""

import unicodedata


CODE_REVIEW_POLICY = "code-basic-v1"


def normalize_option(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def code_quality(item: dict, abstract: str) -> dict:
    """Check source-string uniqueness, never semantic answer uniqueness.

    Compact submissions derive the blank from the designated answer. This is
    therefore a source-consistency check, not independent answer adjudication.
    Existing source/span/input integrity requirements remain prerequisites.
    """
    options = item.get("options")
    options = options if isinstance(options, list) else []
    records_valid = all(
        isinstance(option, dict) and set(option) == {"id", "text"}
        and isinstance(option.get("id"), str) and option["id"] in "ABCD"
        and len(option["id"]) == 1
        and isinstance(option.get("text"), str) and 0 < len(option["text"]) <= 1000
        and bool(option["text"].strip()) for option in options
    )
    texts = [option["text"] for option in options
             if isinstance(option, dict) and isinstance(option.get("text"), str) and option["text"].strip()]
    formatted = records_valid and len(options) == 4 and {option["id"] for option in options} == set("ABCD")
    normalized = [normalize_option(text) for text in texts]
    distinct = len(texts) == len(options) and bool(texts) and len(set(normalized)) == len(normalized)
    blank = item.get("blank_span")
    blank_valid = (
        isinstance(blank, dict) and type(blank.get("start")) is int and type(blank.get("end")) is int
        and 0 <= blank["start"] < blank["end"] <= len(abstract)
    )
    source = abstract[blank["start"]:blank["end"]] if blank_valid else None
    designated = [option for option in options if isinstance(option, dict)
                  and isinstance(item.get("answer_id"), str)
                  and item["answer_id"] in {"A", "B", "C", "D"}
                  and option.get("id") == item["answer_id"]]
    answer_valid = len(designated) == 1 and source is not None and designated[0].get("text") == source
    source_matches = [option.get("id") for option in options if isinstance(option, dict)
                      and isinstance(option.get("id"), str) and option["id"] in {"A", "B", "C", "D"}
                      and isinstance(option.get("text"), str) and source is not None
                      and normalize_option(option["text"]) == normalize_option(source)]
    unique = source is not None and len(source_matches) == 1
    checks = [
        {"key": "option_format", "label": "보기 형식", "passed": formatted,
         "message": "비어 있지 않은 보기 4개입니다." if formatted else "1~1000자의 비어 있지 않은 문자열 보기 4개가 필요합니다."},
        {"key": "no_duplicates", "label": "중복 여부", "passed": distinct,
         "message": "정규화한 보기 문자열이 서로 다릅니다." if distinct else "대소문자·공백·유니코드 정규화 후 중복되거나 확인할 수 없는 보기가 있습니다."},
        {"key": "designated_answer", "label": "지정 정답", "passed": answer_valid,
         "message": "지정 정답이 유효하며 원문 빈칸을 정확히 복원합니다." if answer_valid else "지정 정답이 유효하지 않거나 원문 빈칸을 정확히 복원하지 못합니다."},
        {"key": "unique_source_answer", "label": "유일한 정답 · 원문 일치", "passed": unique,
         "message": "원문 빈칸과 일치하는 보기가 정확히 하나입니다. 의미적 유일성은 미검사입니다." if unique else "원문 빈칸과 일치하는 보기가 정확히 하나인지 확인하지 못했습니다."},
    ]
    passed = bool(item.get("structure_valid")) and all(check["passed"] for check in checks)
    reasons = [check["message"] for check in checks if not check["passed"]]
    reasons.extend(issue["message"] for issue in item.get("issues", []))
    return {
        "status": "complete", "source": "code", "q": 1.0 if passed else 0.0,
        "result": "pass" if passed else "fail", "checks": checks,
        "source_match_option_ids": source_matches, "semantic_reviewed": False,
        "accepted_option_ids": [], "option_assessments": [], "rubric": None, "gates": None,
        "rationale": "코드 검사 4개 항목을 통과했습니다. 문맥 충분성과 의미적 정답성은 검사하지 않았습니다." if passed else " / ".join(dict.fromkeys(reasons)) or "입력 구조 검증에 실패했습니다.",
        "reviewer": None, "review_recommended": False, "scoring_policy": CODE_REVIEW_POLICY,
    }
