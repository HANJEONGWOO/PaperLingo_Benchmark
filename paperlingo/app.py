"""Self-hosted API and static application for PaperLingo."""
import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
import copy
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import secrets
import time
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings, ROOT
from .code_review import CODE_REVIEW_POLICY
from .dataset import Dataset
from .judge import JudgeConfig, judge_item, quality_from_review, temporary_quality, QUALITY_POLICY_VERSION
from .scoring import METRICS_VERSION, SubmissionError, evaluate, recompute
from .storage import Store, now, report_status

VERSION = "0.10.0"
COOKIE = "paperlingo_session"


def strict_json(value):
    def reject_constant(value):
        raise ValueError("JSON에는 NaN 또는 Infinity를 사용할 수 없습니다.")

    def unique_pairs(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("JSON 객체에 중복된 필드가 있습니다.")
            obj[key] = value
        return obj

    return json.loads(value, parse_constant=reject_constant, object_pairs_hook=unique_pairs)


def validate_json_tree(value):
    pending = [(value, 0)]
    while pending:
        obj, depth = pending.pop()
        if depth > 30:
            raise ValueError("JSON 중첩이 너무 깊습니다.")
        if isinstance(obj, float) and not math.isfinite(obj):
            raise ValueError("유한하지 않은 숫자는 사용할 수 없습니다.")
        if isinstance(obj, dict):
            pending.extend((key, depth + 1) for key in obj)
            pending.extend((v, depth + 1) for v in obj.values())
        elif isinstance(obj, list):
            pending.extend((v, depth + 1) for v in obj)
        elif isinstance(obj, str):
            try:
                obj.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("JSON 문자열에 유효하지 않은 유니코드 문자가 있습니다.") from None


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=1024)


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    scope: Literal["demo", "evaluation"] | None = None
    paper_ids: list[str] | None = None
    predictions: str | list[dict[str, Any]]


def parse_predictions(value):
    if isinstance(value, list):
        try:
            validate_json_tree(value)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return value
    if not value.strip():
        raise HTTPException(422, "생성 결과가 비어 있습니다. 빈 제출은 []로 명시하세요.")
    try:
        parsed = strict_json(value)
    except json.JSONDecodeError:
        parsed = []
        for line_number, line in enumerate(value.splitlines(), 1):
            if not line.strip():
                continue
            try:
                parsed.append(strict_json(line))
            except (ValueError, RecursionError):
                raise HTTPException(422, f"JSONL {line_number}행의 JSON 문법을 확인하세요.") from None
    except (ValueError, RecursionError) as exc:
        raise HTTPException(422, "올바른 JSON 형식인지 확인하세요. 중복 필드와 NaN/Infinity는 허용하지 않습니다.") from None
    if isinstance(parsed, dict) and "paper_id" in parsed:
        parsed = [parsed]
    if not isinstance(parsed, list) or any(not isinstance(p, dict) for p in parsed):
        raise HTTPException(422, "논문별 JSON 객체의 배열 또는 JSONL을 제출하세요.")
    try:
        validate_json_tree(parsed)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return parsed


def csv_value(value):
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


def create_app(settings: Settings | None = None):
    cfg = settings or Settings.from_env()
    cfg.validate()
    dataset = Dataset(cfg.data_dir)
    public_reference_ids = frozenset(dataset.splits["demo"])
    store = Store(cfg.runtime_dir)
    jobs: dict[str, asyncio.Task] = {}
    all_tasks: set[asyncio.Task] = set()
    judge_lock = asyncio.Lock()
    write_lock = asyncio.Lock()
    login_attempts = defaultdict(deque)
    upload_attempts = defaultdict(deque)
    judge_health_cache = {"checked": 0.0, "value": None}

    def public_run(run):
        """Omit held-out reference text at every response boundary, not in storage.

        Only public examples may expose reference text. Papers excluded by the
        length filter stay private, including in historical mixed scopes. Keep
        predictions, match feedback and counts for result analysis.
        """
        papers = []
        for paper in run["report"]["papers"]:
            if paper["paper_id"] not in public_reference_ids:
                paper = dict(paper)
                paper["reference_hidden"] = True
                paper["gold_count"] = len(paper.pop("gold_expressions", []))
                paper["missing_count"] = len(paper.pop("missing_expressions", []))
            papers.append(paper)
        return {**run, "report": {**run["report"], "papers": papers}}

    @asynccontextmanager
    async def lifespan(app):
        store.recover_interrupted()
        if cfg.review_mode == "llm" and cfg.judge_configured:
            # Resume interrupted work and finish legacy records that were left
            # waiting for manual review. Completed reviews are never selected.
            async with write_lock:
                for summary in reversed(store.summaries(cfg.max_runs)):
                    if summary["status"] in {"queued", "awaiting_review", "needs_review"}:
                        run = store.get(summary["id"])
                        if run["report"]["dataset_version"] == dataset.version:
                            enqueue_judge(run, automatic=True)
                        elif run["status"] == "queued":
                            run["status"] = "error"
                            run["judge_meta"]["error"] = "데이터셋 버전이 변경되어 자동 검수를 재개하지 못했습니다. 새 평가를 생성하세요."
                            store.save(run)
        else:
            for summary in store.summaries(cfg.max_runs):
                if summary["status"] == "queued":
                    run = store.get(summary["id"])
                    run["status"] = report_status(run["report"])
                    store.save(run)
        yield
        tasks = list(all_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="PaperLingo Benchmark", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.dataset, app.state.store, app.state.settings, app.state.jobs = dataset, store, cfg, jobs
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(cfg.allowed_hosts))

    def is_admin(request):
        if cfg.public_access:
            return True
        if not cfg.password:
            return bool(request.client and request.client.host in {"127.0.0.1", "::1", "testclient"})
        return store.valid_session(request.cookies.get(COOKIE))

    def need_read(request):
        if not (cfg.public_read or cfg.public_access or is_admin(request)):
            raise HTTPException(401, "팀 비밀번호로 로그인하세요.")

    def need_admin(request):
        if not is_admin(request):
            raise HTTPException(401, "이 작업에는 관리자 로그인이 필요합니다.")

    def get_run(run_id):
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, "평가 기록을 찾을 수 없습니다.")
        return run

    @app.middleware("http")
    async def browser_guard(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.headers.get("X-Requested-With") != "PaperLingo":
                return JSONResponse({"detail": "X-Requested-With: PaperLingo 헤더가 필요합니다."}, 403)
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme != request.url.scheme or parsed.netloc.lower() != request.url.netloc.lower():
                    return JSONResponse({"detail": "다른 사이트에서 보낸 변경 요청은 허용하지 않습니다."}, 403)
            try:
                declared = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "잘못된 Content-Length입니다."}, 400)
            if declared > cfg.max_request_bytes:
                return JSONResponse({"detail": "업로드는 16MB 이하여야 합니다."}, 413)
            chunks, total = [], 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > cfg.max_request_bytes:
                    return JSONResponse({"detail": "업로드는 16MB 이하여야 합니다."}, 413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
            if request._body:
                if "application/json" not in request.headers.get("content-type", ""):
                    return JSONResponse({"detail": "Content-Type: application/json으로 요청하세요."}, 415)
                try:
                    validate_json_tree(strict_json(request._body))
                except (ValueError, RecursionError):
                    return JSONResponse({"detail": "올바른 JSON을 제출하세요. 중복 필드·NaN·Infinity·과도한 중첩은 허용하지 않습니다."}, 422)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path == "/" or (
            request.url.path.startswith("/static/")
            and Path(request.url.path).suffix in {".html", ".js", ".css"}
        ):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": VERSION}

    @app.get("/api/session")
    def session(request: Request):
        admin = is_admin(request)
        return {"authenticated": admin, "auth_required": bool(cfg.password) and not cfg.public_access, "can_manage": admin, "public_read": cfg.public_read or cfg.public_access, "public_access": cfg.public_access}

    @app.post("/api/login")
    async def login(body: LoginInput, request: Request):
        client = request.client.host if request.client else "unknown"
        attempts = login_attempts[client]
        clock = time.monotonic()
        while attempts and attempts[0] < clock - 60:
            attempts.popleft()
        if len(attempts) >= 5:
            raise HTTPException(429, "로그인 시도가 많습니다. 1분 후 다시 시도하세요.")
        attempts.append(clock)
        if not cfg.password or not secrets.compare_digest(hashlib.sha256(body.password.encode()).digest(), hashlib.sha256(cfg.password.encode()).digest()):
            raise HTTPException(401, "비밀번호가 올바르지 않습니다.")
        attempts.clear()
        response = JSONResponse({"authenticated": True, "can_manage": True})
        response.set_cookie(COOKIE, store.create_session(cfg.session_hours), httponly=True, secure=cfg.secure_cookie, samesite="strict", max_age=cfg.session_hours * 3600, path="/")
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        store.remove_session(request.cookies.get(COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/config")
    def config(request: Request):
        need_read(request)
        return {"dataset": dataset.summary(), "review_mode": cfg.review_mode,
                "quality_policy_version": CODE_REVIEW_POLICY if cfg.review_mode == "code" else QUALITY_POLICY_VERSION,
                "judge": {"configured": cfg.judge_configured, "automatic": cfg.review_mode == "llm" and cfg.judge_configured, "model": cfg.judge_model, "base_url": cfg.judge_base_url, "concurrency": cfg.judge_concurrency}, "auth_required": bool(cfg.password) and not cfg.public_access, "public_read": cfg.public_read or cfg.public_access, "public_access": cfg.public_access, "can_manage": is_admin(request), "limits": {"max_upload_bytes": cfg.max_request_bytes, "judge_batch_size": cfg.judge_batch_size, "max_runs": cfg.max_runs}, "metrics_version": METRICS_VERSION, "version": VERSION}

    @app.get("/api/judge/status")
    async def judge_status(request: Request):
        need_read(request)
        if cfg.review_mode == "code":
            return {"mode": "code", "configured": False, "ready": True, "model": "",
                    "message": "코드로 4개 항목을 즉시 검사합니다. LLM과 문맥 충분성 검사는 사용하지 않습니다."}
        if not cfg.judge_configured:
            return {"configured": False, "ready": False, "model": "", "message": "LLM 평가 모델이 설정되지 않았습니다."}
        if judge_health_cache["value"] is not None and time.monotonic() - judge_health_cache["checked"] < 5:
            return judge_health_cache["value"]
        endpoint = cfg.judge_base_url.removesuffix("/chat/completions").rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {cfg.judge_api_key}"} if cfg.judge_api_key else {}
        value = {"configured": True, "ready": False, "model": cfg.judge_model, "message": "평가 모델을 불러오는 중이거나 로컬 서버가 중지되어 있습니다."}
        try:
            async with httpx.AsyncClient(timeout=2, follow_redirects=False) as http:
                response = await http.get(endpoint, headers=headers)
                if response.status_code == 200:
                    records = response.json().get("data", [])
                    value["ready"] = any(isinstance(row, dict) and row.get("id") == cfg.judge_model for row in records)
                    value["message"] = "평가 모델이 준비되었습니다." if value["ready"] else "설정된 모델을 추론 서버에서 찾을 수 없습니다."
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            pass
        judge_health_cache.update(checked=time.monotonic(), value=value)
        return value

    @app.get("/api/splits")
    def splits(request: Request, download: bool = False):
        need_read(request)
        manifest = {"dataset_version": dataset.version, "split_version": dataset.SPLIT_VERSION,
                    "splits": dataset.splits, "example_answers": dataset.example_answer_metadata(),
                    "evaluation_filter": dataset.evaluation_filter,
                    "evaluation_sentence_counts": {pid: dataset.sentence_counts[pid] for pid in dataset.splits["evaluation"]}}
        if download:
            return Response(json.dumps(manifest, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="paperlingo-splits.json"'})
        return manifest

    @app.get("/api/papers")
    def papers(request: Request, q: str = "", offset: int = 0, limit: int = 20, domain: str = "",
               scope: Literal["demo", "evaluation"] = "demo"):
        need_read(request)
        q = q[:200].casefold()
        rows = []
        for pid in dataset.splits[scope]:
            paper = dataset.papers[pid]
            dom = paper.get("domain", {})
            code = dom.get("code", "") if isinstance(dom, dict) else str(dom)
            if domain and domain != code:
                continue
            if q and q not in f"{pid} {paper['paper_name']} {code}".casefold():
                continue
            row = {"paper_id": pid, "paper_name": paper["paper_name"], "domain": code}
            if pid in public_reference_ids:
                row["gold_count"] = len(dataset.labels[pid])
            rows.append(row)
        offset, limit = max(offset, 0), max(1, min(limit, 100))
        return {"items": rows[offset:offset + limit], "total": len(rows), "scope": scope}

    @app.get("/api/papers/{paper_id:path}")
    def paper_detail(paper_id: str, request: Request):
        need_read(request)
        if paper_id not in dataset.papers:
            raise HTTPException(404, "논문을 찾을 수 없습니다.")
        paper = dict(dataset.papers[paper_id])
        if paper_id in public_reference_ids:
            paper["expressions"] = dataset.labels[paper_id]
        else:
            paper["reference_hidden"] = True
        return paper

    @app.get("/api/datasets/{scope}/{kind}")
    def download_dataset(request: Request, scope: Literal["demo", "evaluation"],
                         kind: Literal["papers", "labels", "answers"]):
        need_read(request)
        if scope == "evaluation" and kind != "papers":
            raise HTTPException(404, "평가 데이터는 논문 초록만 제공합니다. 선정 표현과 사지선다 예시답안은 비공개입니다.")
        paper_ids = dataset.splits[scope]
        if kind == "papers":
            fields = ("paper_id", "paper_name", "domain", "categories", "abstract")
            records = [{key: dataset.papers[pid][key] for key in fields if key in dataset.papers[pid]}
                       for pid in paper_ids]
        elif kind == "labels":
            records = [{"paper_id": pid, "expressions": dataset.labels[pid]} for pid in paper_ids]
        else:
            records = dataset.demo_predictions(compact=True)
        content = "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"
        headers = {
            "Content-Disposition": f'attachment; filename="paperlingo-{"example" if scope == "demo" else scope}-{kind}.jsonl"',
            "X-Dataset-Version": dataset.version,
            "X-Split-Version": dataset.SPLIT_VERSION,
        }
        if kind == "answers":
            metadata = dataset.example_answer_metadata()
            headers.update({"X-Answer-Version": metadata["version"], "X-Answer-Quality": metadata["quality_status"]})
        return Response(content, media_type="application/x-ndjson", headers=headers)

    @app.get("/api/sample")
    def sample(request: Request, download: bool = False):
        need_read(request)
        predictions = dataset.demo_predictions(compact=True)
        if download:
            content = "\n".join(json.dumps(p, ensure_ascii=False) for p in predictions) + "\n"
            return Response(content, media_type="application/x-ndjson", headers={"Content-Disposition": 'attachment; filename="paperlingo-format-example.jsonl"'})
        return {"scope": "demo", "paper_ids": dataset.splits["demo"], "predictions": predictions, "note": "참조 표현을 모두 담아 직접 작성한 형식 예제입니다. 형식 예제 세트에서 Precision·Recall·F1은 100%이며 보기 품질은 별도 평가합니다. 모델의 실측 성능이 아닙니다."}

    @app.get("/api/schema")
    def schema(request: Request, lang: Literal["ko", "en"] = "ko"):
        need_read(request)
        definition = {
            "version": VERSION,
            "submission_format": "compact-v1",
            "input": "논문별 객체의 JSON 배열 또는 JSONL",
            "max_questions_per_paper": 5,
            "scopes": ["demo", "evaluation"],
            "scope_optional": True,
            "scope_selection": "automatic_when_omitted",
            "required_paper_fields": ["paper_id", "questions"],
            "required_question_fields": ["expression_text", "options", "answer_id"],
            "question_fields": {
                "expression_text": {"type": "string", "description": "원본 abstract에 있는 학습 표현 전체"},
                "options": {"type": "array", "items": "string", "length": 4, "description": "서로 다른 보기 문자열 4개"},
                "answer_id": {"type": "integer", "minimum": 0, "maximum": 3, "description": "options 배열의 정답 인덱스. 0은 첫 번째 보기이며 문자열과 boolean은 허용하지 않음"},
            },
            "context_rule": "원문에서 expression_text가 온전한 단어로 처음 등장하는 위치를 사용합니다. 표현 안에서 options[answer_id]가 온전한 단어로 처음 등장하는 부분을 빈칸으로 만들고, 표현 전체를 포함하는 원문 문맥을 자동 추출합니다.",
            "server_generated": ["question_id", "expression_span", "context_span", "blank_span"],
            "legacy_supported": True,
            "sample": dataset.demo_predictions(compact=True)[0],
        }
        if lang == "en":
            definition["input"] = "A JSON array or JSONL of paper objects"
            descriptions = {
                "expression_text": "The entire learning expression from the source abstract",
                "options": "Four distinct option strings",
                "answer_id": "The answer index in the options array. 0 identifies the first option; strings and booleans are not allowed",
            }
            for field, description in descriptions.items():
                definition["question_fields"][field]["description"] = description
            definition["context_rule"] = "Use the first whole-word occurrence of expression_text in the source. Blank out the first whole-word occurrence of options[answer_id] within that expression and extract context containing the entire expression."
        return definition

    @app.get("/api/runs")
    def runs(request: Request):
        need_read(request)
        return {"items": store.summaries(cfg.max_runs)}

    @app.post("/api/runs", status_code=201)
    async def new_run(body: RunInput, request: Request):
        need_admin(request)
        if cfg.public_access:
            clock = time.monotonic()
            key = request.client.host if request.client else "unknown"
            attempts = upload_attempts[key]
            while attempts and attempts[0] < clock - 60:
                attempts.popleft()
            if len(attempts) >= 10:
                raise HTTPException(429, "한 번에 너무 많은 평가가 제출되었습니다. 1분 후 다시 시도하세요.")
            attempts.append(clock)
        predictions = parse_predictions(body.predictions)
        if not body.name.strip():
            raise HTTPException(422, "평가 이름을 입력하세요.")
        scope = body.scope
        if scope is None:
            submitted_ids = [prediction.get("paper_id") for prediction in predictions]
            valid_ids = {pid for pid in submitted_ids if isinstance(pid, str)}
            if valid_ids & public_reference_ids and valid_ids & set(dataset.splits["evaluation"]):
                raise HTTPException(422, "예시 데이터와 평가 데이터가 섞여 있습니다. 각각 별도 파일로 제출하세요.")
            scope = "demo" if submitted_ids and all(
                isinstance(pid, str) and pid in public_reference_ids for pid in submitted_ids
            ) else "evaluation"
        paper_ids = dataset.splits[scope]
        if body.paper_ids is not None and (
            len(body.paper_ids) != len(paper_ids) or set(body.paper_ids) != set(paper_ids)
        ):
            raise HTTPException(422, "고정 평가 범위와 paper_ids가 다릅니다.")
        try:
            report = evaluate(dataset, predictions, paper_ids, scope)
        except SubmissionError as exc:
            raise HTTPException(422, {"message": "제출 형식을 확인하세요.", "errors": exc.errors}) from None
        report["split_version"] = dataset.SPLIT_VERSION
        if cfg.review_mode == "code":
            report["quality_policy_version"] = CODE_REVIEW_POLICY
            report["review_mode"] = "code"
            recompute(report)
        async with write_lock:
            if store.count() >= cfg.max_runs:
                raise HTTPException(409, "평가 기록 저장 한도에 도달했습니다. 관리자가 백업 후 저장소를 정리해야 합니다.")
            run = store.create(body.name.strip(), report, predictions == dataset.demo_predictions(compact=True) or predictions == dataset.demo_predictions())
            if cfg.review_mode == "llm" and cfg.judge_configured:
                enqueue_judge(run, automatic=True)
            return public_run(run)

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str, request: Request):
        need_read(request)
        return public_run(get_run(run_id))

    def pending_items(run):
        return [(p["paper_id"], copy.deepcopy(i)) for p in run["report"]["papers"] for i in p["items"]
                if i["structure_valid"] and (i["quality"]["status"] in {"pending", "needs_review", "error"}
                                             or i["quality"].get("source") == "temporary")]

    def enqueue_judge(run, automatic=False):
        """Called under write_lock; persist the queue before starting its task."""
        selected = pending_items(run)
        if not selected:
            run["status"] = report_status(run["report"])
            store.save(run)
            return False
        job_id = secrets.token_hex(12)
        run["status"] = "queued"
        # Review actions change quality only. Keep the run's original formula;
        # switching a legacy run to the new formula would rewrite its meaning.
        run["report"]["quality_policy_version"] = QUALITY_POLICY_VERSION
        run["judge_meta"] = {"model": cfg.judge_model, "job_id": job_id, "automatic": automatic,
                             "queued_at": now(), "total": len(selected), "processed": 0,
                             "remaining_after_batch": 0}
        recompute(run["report"])
        store.save(run)
        task = asyncio.create_task(do_judge(run["id"], job_id, selected))
        jobs[run["id"]] = task
        all_tasks.add(task)
        task.add_done_callback(all_tasks.discard)
        return True

    def current_job(run, job_id):
        return run["judge_meta"].get("job_id") == job_id and run["status"] in {"queued", "judging"}

    async def do_judge(run_id, job_id, selected):
        semaphore = asyncio.Semaphore(cfg.judge_concurrency)
        judge_cfg = JudgeConfig(base_url=cfg.judge_base_url, api_key=cfg.judge_api_key, model=cfg.judge_model, timeout=cfg.judge_timeout)

        async def one(pid, item):
            async with semaphore:
                async with write_lock:
                    if not current_job(get_run(run_id), job_id):
                        return
                quality = await judge_item(item, judge_cfg)
            async with write_lock:
                run = get_run(run_id)
                # Skip returns immediately, but an in-flight GPU call drains
                # before the next run starts. Its late result cannot overwrite
                # temporary scores or an optional human correction.
                if not current_job(run, job_id):
                    return
                for paper in run["report"]["papers"]:
                    if paper["paper_id"] == pid:
                        for current in paper["items"]:
                            if current["question_id"] == item["question_id"]:
                                previous = copy.deepcopy(current["quality"])
                                current["quality"] = quality
                                break
                recompute(run["report"])
                run["judge_meta"]["processed"] = run["judge_meta"].get("processed", 0) + 1
                store.save(run, {"paper_id": pid, "question_id": item["question_id"], "previous_quality": previous, "quality": quality})

        try:
            async with judge_lock:
                async with write_lock:
                    run = get_run(run_id)
                    if not current_job(run, job_id):
                        return
                    run["status"] = "judging"
                    run["judge_meta"]["started_at"] = now()
                    store.save(run)
                for offset in range(0, len(selected), cfg.judge_batch_size):
                    async with write_lock:
                        if not current_job(get_run(run_id), job_id):
                            return
                    batch = selected[offset:offset + cfg.judge_batch_size]
                    outcomes = await asyncio.gather(*(one(pid, item) for pid, item in batch), return_exceptions=True)
                    if any(isinstance(outcome, BaseException) for outcome in outcomes):
                        raise RuntimeError("One or more judge tasks could not be completed")
        except asyncio.CancelledError:
            async with write_lock:
                run = get_run(run_id)
                if current_job(run, job_id):
                    run["judge_meta"]["interrupted"] = True
                    run["status"] = "queued"
                    store.save(run)
            raise
        except Exception:
            # Never place provider responses or credentials in public errors.
            async with write_lock:
                run = get_run(run_id)
                if current_job(run, job_id):
                    run["judge_meta"]["error"] = "평가 작업이 중단되었습니다. 미완료 문항을 다시 실행하세요."
                    run["status"] = "error"
                    store.save(run)
        else:
            async with write_lock:
                run = get_run(run_id)
                if current_job(run, job_id):
                    run["status"] = report_status(run["report"])
                    run["judge_meta"]["finished_at"] = now()
                    store.save(run)
        finally:
            if jobs.get(run_id) is asyncio.current_task():
                jobs.pop(run_id, None)

    @app.post("/api/runs/{run_id}/judge")
    async def start_judge(run_id: str, request: Request):
        need_admin(request)
        if cfg.review_mode == "code":
            raise HTTPException(409, "현재는 코드 검사 모드입니다. LLM 검수는 실행하지 않습니다.")
        if not cfg.judge_configured:
            raise HTTPException(409, "서버의 JUDGE_BASE_URL과 JUDGE_MODEL을 설정하세요. 필요하면 JUDGE_API_KEY도 설정하세요.")
        async with write_lock:
            run = get_run(run_id)
            if run["report"].get("quality_policy_version") == CODE_REVIEW_POLICY:
                raise HTTPException(409, "코드 검사 기록은 LLM 판정으로 변경하지 않습니다. 다른 방식의 검수는 새 평가로 생성하세요.")
            if run["report"]["dataset_version"] != dataset.version:
                raise HTTPException(409, "데이터셋 버전이 변경되었습니다. 새 평가 기록을 생성하세요.")
            if run["status"] in {"queued", "judging"}:
                return {"status": run["status"], "count": run["judge_meta"].get("total", 0), "remaining_after_batch": 0}
            if not enqueue_judge(run):
                raise HTTPException(409, "LLM 평가가 필요한 문항이 없습니다.")
        return {"status": "queued", "count": run["judge_meta"]["total"], "remaining_after_batch": 0}

    @app.post("/api/runs/{run_id}/skip-judge")
    async def skip_judge(run_id: str, request: Request):
        need_admin(request)
        async with write_lock:
            run = get_run(run_id)
            if run["report"].get("quality_policy_version") == CODE_REVIEW_POLICY:
                raise HTTPException(409, "코드 검사는 이미 완료되었습니다. 검사 결과를 임시 만점으로 덮어쓸 수 없습니다.")
            queued_task = jobs.get(run_id) if run["status"] == "queued" else None
            valid = [(p, i) for p in run["report"]["papers"] for i in p["items"] if i["structure_valid"]]
            if not valid:
                raise HTTPException(409, "임시 품질 만점을 적용할 구조 유효 문항이 없습니다.")
            reviews = []
            for paper, item in valid:
                if item["quality"].get("source") == "temporary":
                    continue
                previous = copy.deepcopy(item["quality"])
                item["quality"] = temporary_quality(item)
                reviews.append({"paper_id": paper["paper_id"], "question_id": item["question_id"],
                                "previous_quality": previous, "quality": item["quality"]})
            run["judge_meta"].update(job_id=None, skipped_at=now(), temporary_full_credit=True)
            run["report"]["quality_policy_version"] = QUALITY_POLICY_VERSION
            recompute(run["report"])
            run["status"] = report_status(run["report"])
            store.save(run, reviews=reviews)
            if queued_task:
                # No inference has started; remove the queue entry immediately.
                queued_task.cancel()
                if jobs.get(run_id) is queued_task:
                    jobs.pop(run_id, None)
            return public_run(run)

    @app.post("/api/runs/{run_id}/reviews")
    async def review(run_id: str, body: dict, request: Request):
        need_admin(request)
        async with write_lock:
            run = get_run(run_id)
            if run["report"].get("quality_policy_version") == CODE_REVIEW_POLICY:
                raise HTTPException(409, "코드 검사 결과는 통과 또는 실패로 고정됩니다. 입력을 수정해 새 평가를 생성하세요.")
            if run["status"] in {"queued", "judging"}:
                raise HTTPException(409, "LLM 평가가 끝난 뒤 수동 검수를 저장하세요.")
            item = next((i for p in run["report"]["papers"] if p["paper_id"] == body.get("paper_id") for i in p["items"] if i["question_id"] == body.get("question_id")), None)
            if item is None:
                raise HTTPException(404, "해당 문항을 찾을 수 없습니다.")
            if not item["structure_valid"]:
                raise HTTPException(422, "구조 오류는 수동 점수로 덮어쓸 수 없습니다. 생성 결과를 수정해 새로 제출하세요.")
            previous = copy.deepcopy(item["quality"])
            payload = {k: v for k, v in body.items() if k not in {"paper_id", "question_id"}}
            try:
                quality = quality_from_review(item, payload, source="human")
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, str(exc)[:500]) from None
            item["quality"] = quality
            recompute(run["report"])
            run["status"] = report_status(run["report"])
            store.save(run, {"paper_id": body["paper_id"], "question_id": body["question_id"], "previous_quality": previous, "quality": quality})
            return public_run(run)

    @app.get("/api/runs/{run_id}/export")
    def export(run_id: str, request: Request, format: Literal["json", "csv"] = "json"):
        need_read(request)
        run = public_run(get_run(run_id))
        if format == "json":
            run["review_history"] = store.review_history(run_id)
            return Response(json.dumps(run, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="paperlingo-{run_id}.json"'})
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["paper_id", "question_id", "expression", "matched", "structure_valid", "quality_status", "quality_source", "quality_score_pct", "accepted_options", "answer_id", "issues", "rationale", "score_mode", "quality_policy", "metrics_version", "check_result", "option_format", "no_duplicates", "designated_answer", "unique_source_answer", "source_match_options"])
        for paper in run["report"]["papers"]:
            if not paper["items"]:
                writer.writerow([csv_value(paper["paper_id"]), "", "", "", "", "empty_submission" if paper["submitted"] else "missing_submission"] + [""] * 6 + [run["report"].get("score_mode", "reviewed"), "", run["report"].get("metrics_version")] + [""] * 6)
            for item in paper["items"]:
                q = item["quality"]
                checks = {check["key"]: check["passed"] for check in q.get("checks", [])}
                writer.writerow([csv_value(x) for x in [paper["paper_id"], item["question_id"], item["expression_text"], item["matched"], item["structure_valid"], q["status"], q["source"], None if q["q"] is None else round(q["q"] * 100, 2), ",".join(q.get("accepted_option_ids", [])), item.get("answer_id", ""), "; ".join(i["message"] for i in item["issues"]), q.get("rationale", ""), run["report"].get("score_mode", "reviewed"), q.get("scoring_policy", ""), run["report"].get("metrics_version"), q.get("result", ""), *[checks.get(key, "") for key in ("option_format", "no_duplicates", "designated_answer", "unique_source_answer")], ",".join(q.get("source_match_option_ids", []))]])
        return Response("\ufeff" + buffer.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="paperlingo-{run_id}.csv"'})

    static = Path(__file__).parent / "static"
    static.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static), name="static")
    index_html = (static / "index.html").read_text(encoding="utf-8")
    for filename in ("styles.css", "translations.js", "i18n.js", "app.js"):
        revision = hashlib.sha256((static / filename).read_bytes()).hexdigest()[:16]
        asset_url = f"/static/{filename}"
        index_html = index_html.replace(f'"{asset_url}"', f'"{asset_url}?v={revision}"')

    @app.get("/")
    def index():
        return HTMLResponse(index_html)

    return app
