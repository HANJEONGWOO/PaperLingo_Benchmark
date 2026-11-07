"""Blind, auditable question-quality judgments via an OpenAI-compatible endpoint.

The model supplies observations, never its own final score. Both human and model
reviews pass through the same strict validator and deterministic scoring gates.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import random
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx


PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "judge_v1.txt"
RUBRIC_KEYS = {"plausibility", "learning_alignment", "distinctness", "no_cues"}
GATE_KEYS = {"context_sufficient", "no_specialist_knowledge", "no_answer_leak"}
VERDICTS = {"acceptable", "incorrect", "uncertain"}


@dataclass(frozen=True)
class JudgeConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout: float = 90
    max_retries: int = 2

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Judge base_url must be an HTTP(S) URL without credentials, query or fragment.")
        if not isinstance(self.api_key, str):
            raise ValueError("Judge api_key must be a string.")
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 200:
            raise ValueError("Judge model must be explicitly configured.")
        if type(self.timeout) not in {int, float} or not 0 < self.timeout <= 300:
            raise ValueError("Judge timeout must be between 0 and 300 seconds.")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 5:
            raise ValueError("Judge max_retries must be an integer between 0 and 5.")

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _text(value: Any, name: str, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name}: nonempty string of at most {limit} characters required.")
    return value.strip()


def _exact_fields(value: Any, keys: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{name}: required fields do not match the review schema.")
    return value


def quality_from_review(item: dict, payload: dict, source: str = "human") -> dict:
    """Validate a review, calculate quality, and return the shared quality record.

    Human payload: accepted_option_ids, rubric, gates, rationale, reviewer.
    LLM payload: option_assessments, rubric, gates, rationale (no answer key).
    API routing fields paper_id/question_id are allowed in human payloads only.
    Invalid schemas raise ValueError. Structurally invalid items cannot be repaired
    through reviews: they must be corrected in a fresh submission.
    """
    if source not in {"human", "llm"}:
        raise ValueError("Review source must be human or llm.")
    if item.get("structure_valid") is not True:
        raise ValueError("Structurally invalid items cannot receive quality overrides.")
    options = item.get("options")
    if (
        not isinstance(options, list)
        or len(options) != 4
        or any(not isinstance(o, dict) or not isinstance(o.get("id"), str) for o in options)
    ):
        raise ValueError("Four known option IDs are required.")
    option_ids = [option["id"] for option in options]
    if len(set(option_ids)) != 4 or item.get("answer_id") not in option_ids:
        raise ValueError("Four distinct option IDs and a valid answer ID are required.")
    if not isinstance(payload, dict):
        raise ValueError("Review must be a JSON object.")
    if source == "human":
        required = {"accepted_option_ids", "rubric", "gates", "rationale", "reviewer"}
        if not required <= payload.keys() or payload.keys() - required - {"paper_id", "question_id"}:
            raise ValueError("Human review fields do not match the schema.")
    else:
        _exact_fields(payload, {"option_assessments", "rubric", "gates", "rationale"}, "review")

    rubric = _exact_fields(payload.get("rubric"), RUBRIC_KEYS, "rubric")
    if any(type(value) is not int or value not in {0, 1, 2} for value in rubric.values()):
        raise ValueError("Rubric scores must be integers 0, 1, or 2; booleans are not scores.")
    gates = _exact_fields(payload.get("gates"), GATE_KEYS, "gates")
    if any(type(value) is not bool for value in gates.values()):
        raise ValueError("Gate values must be JSON booleans.")
    rationale = _text(payload.get("rationale"), "rationale")
    if source == "human":
        reviewer = _text(payload.get("reviewer"), "reviewer", 200)
        accepted = payload.get("accepted_option_ids")
        if (
            not isinstance(accepted, list)
            or any(not isinstance(value, str) or value not in option_ids for value in accepted)
            or len(set(accepted)) != len(accepted)
        ):
            raise ValueError("accepted_option_ids must contain distinct known option IDs.")
        assessments = [
            {"id": option_id, "verdict": "acceptable" if option_id in accepted else "incorrect", "reason": rationale}
            for option_id in option_ids
        ]
    else:
        reviewer = None
        raw_assessments = payload.get("option_assessments")
        if not isinstance(raw_assessments, list) or len(raw_assessments) != 4:
            raise ValueError("Every option must have exactly one assessment.")
        by_id = {}
        for assessment in raw_assessments:
            _exact_fields(assessment, {"id", "verdict", "reason"}, "option assessment")
            option_id = assessment["id"]
            if not isinstance(option_id, str) or option_id not in option_ids or option_id in by_id:
                raise ValueError("Every known option ID must occur exactly once.")
            verdict = assessment["verdict"]
            if not isinstance(verdict, str) or verdict not in VERDICTS:
                raise ValueError("Option verdict must be acceptable, incorrect, or uncertain.")
            by_id[option_id] = {
                "id": option_id,
                "verdict": verdict,
                "reason": _text(assessment["reason"], "option reason", 2000),
            }
        assessments = [by_id[option_id] for option_id in option_ids]

    accepted = [a["id"] for a in assessments if a["verdict"] == "acceptable"]
    uncertain = any(a["verdict"] == "uncertain" for a in assessments)
    valid = accepted == [item["answer_id"]] and all(gates.values())
    return {
        "status": "needs_review" if uncertain else "complete",
        "source": source,
        "q": None if uncertain else sum(rubric.values()) / 8 if valid else 0.0,
        "accepted_option_ids": accepted,
        "rubric": dict(rubric),
        "gates": dict(gates),
        "option_assessments": assessments,
        "rationale": rationale,
        "reviewer": reviewer,
    }


def _error_quality(code: str, metadata: dict) -> dict:
    messages = {
        "transport_error": "평가 서버 연결에 실패했습니다. 서버 설정을 확인한 뒤 다시 시도해 주세요.",
        "provider_error": "평가 서버가 요청을 처리하지 못했습니다. 모델·접속 설정을 확인해 주세요.",
        "invalid_response": "평가 응답이 정해진 JSON 형식과 일치하지 않습니다. 재평가가 필요합니다.",
        "invalid_item": "평가할 문항의 구조가 올바르지 않습니다.",
        "prompt_error": "평가 프롬프트 파일을 읽을 수 없습니다.",
    }
    return {
        "status": "error", "source": "llm", "q": None,
        "accepted_option_ids": [], "rubric": None, "gates": None,
        "option_assessments": [], "rationale": messages[code], "reviewer": None,
        "error_code": code, "metadata": metadata,
    }


def _json_format_unsupported(response: httpx.Response) -> bool:
    """Recognize compatibility errors without returning any provider text."""
    if response.status_code not in {400, 422}:
        return False
    try:
        error = response.json().get("error", {})
        if not isinstance(error, dict):
            return False
        message = str(error.get("message", "")).lower()
        parameter = error.get("param")
        return (parameter == "response_format" or "response_format" in message) and any(
            word in message for word in ("unsupported", "not supported", "unknown", "unrecognized", "not allowed")
        )
    except (ValueError, AttributeError):
        return False


def _usage(response: dict) -> dict:
    raw = response.get("usage", {})
    if not isinstance(raw, dict):
        return {}
    return {
        key: raw[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if type(raw.get(key)) is int and raw[key] >= 0
    }


def _response_identity(response: dict) -> dict:
    """Keep only bounded provider identity tokens, never arbitrary response text."""
    result = {}
    for remote_key, local_key in (("model", "response_model"), ("system_fingerprint", "system_fingerprint")):
        value = response.get(remote_key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:+-]{1,200}", value):
            result[local_key] = value
    return result


def strict_json_loads(content: str) -> Any:
    """Reject duplicate keys and non-JSON numeric constants instead of repairing."""
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict:
        value = {}
        for key, entry in pairs:
            if key in value:
                raise ValueError("Duplicate JSON object key.")
            value[key] = entry
        return value

    def reject_constant(value: str) -> None:
        raise ValueError("Non-JSON numeric constant.")

    return json.loads(content, object_pairs_hook=pairs_hook, parse_constant=reject_constant)


async def judge_item(item: dict, config: JudgeConfig) -> dict:
    """Judge one valid item. Errors are unresolved quality, never zero scores.

    Only learner-visible masked context, shuffled option text/IDs, and learner
    level are transmitted. Answer keys, gold labels and unmasked source stay local.
    Redirects are disabled so authorization is never forwarded to another host.
    """
    start = time.monotonic()
    metadata = {"model": config.model, "prompt_version": "judge-v1", "attempts": 0, "usage": {}}

    def finish(quality: dict) -> dict:
        metadata["latency_ms"] = round((time.monotonic() - start) * 1000)
        quality["metadata"] = dict(metadata)
        return quality

    if item.get("structure_valid") is not True or not isinstance(item.get("context"), str):
        return finish(_error_quality("invalid_item", metadata))
    try:
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return finish(_error_quality("prompt_error", metadata))
    metadata["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    try:
        options = [{"id": option["id"], "text": option["text"]} for option in item["options"]]
        seed_content = json.dumps([item.get("question_id", ""), item["context"], options], ensure_ascii=False, sort_keys=True)
        rng = random.Random(hashlib.sha256(seed_content.encode("utf-8")).hexdigest())
        rng.shuffle(options)
        metadata["option_order"] = [option["id"] for option in options]
        candidate = {"learner_level": "CEFR B1–B2", "context": item["context"], "options": options}
        body = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(candidate, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }
    except (KeyError, TypeError, ValueError):
        return finish(_error_quality("invalid_item", metadata))
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    retry_count = 0
    format_fallback = False
    try:
        async with httpx.AsyncClient(timeout=config.timeout, follow_redirects=False) as client:
            while True:
                metadata["attempts"] += 1
                try:
                    response = await client.post(config.endpoint, headers=headers, json=body)
                except httpx.TransportError:
                    if retry_count < config.max_retries:
                        await asyncio.sleep(min(2 ** retry_count, 4))
                        retry_count += 1
                        continue
                    return finish(_error_quality("transport_error", metadata))
                if not format_fallback and _json_format_unsupported(response):
                    format_fallback = True
                    metadata["json_mode_fallback"] = True
                    body.pop("response_format", None)
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    if retry_count < config.max_retries:
                        await asyncio.sleep(min(2 ** retry_count, 4))
                        retry_count += 1
                        continue
                if not response.is_success:
                    metadata["http_status"] = response.status_code
                    return finish(_error_quality("provider_error", metadata))
                try:
                    result = response.json()
                    if not isinstance(result, dict):
                        raise ValueError("Response must be an object.")
                    metadata["usage"] = _usage(result)
                    metadata.update(_response_identity(result))
                    choices = result["choices"]
                    if not isinstance(choices, list) or len(choices) != 1:
                        raise ValueError("Expected one completion.")
                    if choices[0].get("finish_reason") not in {None, "stop"}:
                        raise ValueError("Completion is truncated or incomplete.")
                    content = choices[0]["message"]["content"]
                    if not isinstance(content, str) or len(content) > 100_000:
                        raise ValueError("Invalid completion content.")
                    payload = strict_json_loads(content)
                    quality = quality_from_review(item, payload, source="llm")
                except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
                    return finish(_error_quality("invalid_response", metadata))
                return finish(quality)
    except (httpx.HTTPError, ValueError):
        return finish(_error_quality("transport_error", metadata))
