# Internal API contract

Self-hosted FastAPI + SQLite + vanilla JS. Korean UI. All metrics are percentages (0..100), quality q is 0..1. No fabricated LLM scores. Original data immutable.

## Engine interface (core owner)

`Dataset(data_dir: Path)` exposes `.papers` (dict by id), `.labels` (dict id -> list of expressions), `.splits` (dict of development/validation/test/demo/all -> list[str]), `.summary()` (paper_count, expression_count, domain_count, zero_gold_count, version, splits counts), `.version` (hash), `.demo_predictions(compact=True)` (public compact examples), `.demo_predictions()` (legacy internal fixtures). Split seed 42 per sorted domain 7/1/2. Demo is a fixed small set including empty-gold paper. `.search(query='', domain='', offset=0, limit=20)` returns paginated summaries.

`evaluate(dataset, predictions: list[dict], paper_ids: list[str], scope_name: str) -> dict` raises `SubmissionError` with `.errors: list[str]` for fatal schema/contract problems. Predictions IDs must be within fixed scope, max5, duplicate paper/question IDs and unknown paper IDs are fatal. Item semantic/structure errors retained as scoreable errors; duplicates expressions count FP. Missing papers retain gold denominator. Input exact text and question structure follows below.

`recompute(report: dict) -> dict` mutates/recomputes metrics after quality update; returns report. Deterministic paper/question matching, no dependency on quality. `validate_review(payload: dict) -> dict` may be implemented by root.

Public question input (`compact-v1`): **expression_text, options, answer_id only**. `options` is an array of exactly four distinct nonempty strings; `answer_id` is an integer 0..3 (not bool, float, numeric string or A/B/C/D). Paper fields remain `paper_id`, `questions`; questions contains 0..5 entries.

Normalization derives stable question IDs, A/B/C/D option IDs and source spans. Select the first exact whole-word occurrence of expression_text in `abstract`, then the first whole-word occurrence of the correct option text inside that expression. The correct text must be an exact substring of expression_text. Use the source sentence interval containing the entire expression; do not cut a spanning expression at a sentence boundary. Replace that answer substring with `[BLANK]` for judging. Repeated occurrences always use the first one; this rule is advertised in the UI and schema. No label information is used to infer spans or choices. Preserve normalization policy metadata with new reports.

Legacy input remains supported for existing clients: question_id, expression_text, expression_type(optional), expression_span/context_span/blank_span {start,end}, options [{id:'A',text},...B/C/D], answer_id(A/B/C/D), learning_point(optional). Internal report/review/judge fields retain that representation. Spans use absolute Python Unicode [start,end) into abstract. Compact choices/answer indices mixed with legacy metadata are explicitly rejected; clients should use one format for each question. Extra fields are rejected. Wrong index/options/source spans are structural failures q=0, retained in the denominator. Missing/non-string expression_text is a fatal submission error. Duplicated expressions get matched=false and q=0 with a duplicate issue.

Report: {metrics_version:'exact-prq-v1', dataset_version, scope:{name,paper_ids,paper_count}, counts:{gold,predicted,matched,submitted_papers,missing_papers,quality_complete,quality_pending,quality_error,needs_review}, metrics:{precision_pct,recall_pct,f1_pct,quality_all_pct,quality_matched_pct,unique_answer_rate_pct,structural_validity_pct,coverage_pct,empty_gold_abstention_pct,final_score}, papers:[{paper_id,paper_name,domain: string,abstract,submitted,gold_expressions:[str],missing_expressions:[str],items:[item]}]}.

Each item uses the normalized internal fields and adds context, matched:bool, structure_valid:bool, issues:[{code,message}], quality:{status:'pending'|'complete'|'needs_review'|'error', source:'structural'|'llm'|'human'|null, q:number|null, accepted_option_ids:[str], rubric:{plausibility:0..2,learning_alignment:0..2,distinctness:0..2,no_cues:0..2}|null, gates:{context_sufficient:bool,no_specialist_knowledge:bool,no_answer_leak:bool}|null, option_assessments:[{id,verdict:'acceptable'|'incorrect'|'uncertain',reason}], rationale:str, reviewer:str|null}}. Initial valid quality pending, invalid structural quality complete/q0. Human and judge must use same fields. Criterion integer scores, gates all true and sole accepted option equals key for nonzero q. Uncertain -> needs_review qnull. HTTP/parse error -> error qnull. No judge result auto-accepts model-issued q.

Aggregate: P=T/N (0 if no N); R=T/G (null if G0); F1=2T/(N+G) (null if both0). Quality all is null until all submitted items complete, matched is null until all matched complete. Final null until every submitted item's quality complete; otherwise 100*P*R*mean(q of matched); T0/G>0 =>0 after all complete; G0 =>null. Zero-submission N0/G>0 final0, quality means null. unique_answer_rate: fraction of all submitted with structurally valid, resolved accepted single key and gates passed; null pending. Structural failures count denominator. Empty-gold abstention requires explicit submitted empty, missing is not correct abstention. Field names exact for UI.

## HTTP contract (root owner)

GET /api/session -> {authenticated,auth_required,can_manage,public_read,csrf_token?}; login POST /api/login {password}, logout POST /api/logout. Cookie session. Mutations require header X-Requested-With: PaperLingo (CSRF same-origin policy). Readonly public can GET, admin can POST. Local no-password session can_manage true. Frontend must not show admin controls unless can_manage.

GET /api/config -> {dataset: summary,judge:{configured,model,base_url,concurrency},auth_required,public_read,can_manage,limits:{max_upload_bytes,max_questions_per_judge_run},version}
GET /api/papers?q=&offset=&limit= -> {items:[{paper_id,paper_name,domain,gold_count}],total}
GET /api/papers/{paper_id} -> original fields + expressions
GET /api/sample -> {scope:'demo',paper_ids:[...],predictions:[compact-v1 papers]}; downloadable fixture via query download=1 returning paperlingo-format-example.jsonl
GET /api/schema -> friendly compact-v1 field contract, answer index bounds, automatic context policy, sample and legacy_supported flag
GET /api/runs -> {items:[run summary]}
POST /api/runs {name,scope:'demo'|'development'|'validation'|'test'|'all'|'custom',paper_ids?:[...],predictions: string JSONL or array} -> full run
GET /api/runs/{id} -> full run
Full run: {id,name,created_at,updated_at,status:'awaiting_review'|'judging'|'complete'|'needs_review'|'error',judge_meta?:{...},report:<above>,is_demo:boolean}. Summary includes {id,name,created_at,status,scope,metrics,counts,is_demo}.
POST /api/runs/{id}/judge -> {status:'judging'} starts pending/error/needs_review items, excludes resolved human/llm. Requires judge config. Limits / server mutex prevent duplicate paid jobs. No auto paid calls on upload.
POST /api/runs/{id}/reviews {paper_id,question_id,accepted_option_ids:[ids],rubric:{...},gates:{...},rationale,reviewer} -> updated full run; structural invalid cannot override, not allowed while judging.
GET /api/runs/{id}/export?format=json|csv -> download.
GET /api/health -> {status:'ok'}

## Judge module (judge owner)

`JudgeConfig(base_url:str,api_key:str,model:str,timeout:float=90,max_retries:int=2)`
`async judge_item(item:dict,config:JudgeConfig) -> dict` returns quality object above with status. Never throws credentials/raw provider text. Server-supplied endpoint only, no browser secrets. Use httpx async chat/completions OpenAI-compatible protocol, response_format JSON optional graceful compatibility. Model and endpoint explicitly set, no assumed model identifier. Prompt no answer key, source text, expression metadata/learning_point that can disclose key. Pass context+options and level only. Shuffle deterministically stable based question/context; preserve option IDs or remap correctly. Prompt treats candidate content as data. Strict validate fields, known option IDs exactly once, bool/int types and bounded values. Uncertain needs_review. Network/invalid responses error, no final score. Judge module owns prompts/judge_v1.txt and tests/test_judge.py and scripts/example_generator.py (optional generator CLI with no labels access). Human review validation can share `quality_from_review(item,payload,source='human')` in judge module returning normalized quality or raises ValueError. Need rationale/reviewer required for manual. Judge q is computed by code.

## UI (frontend owner)

Own paperlingo/static/index.html, app.js, styles.css. No build tool or CDN dependencies. Polished Korean research dashboard, sidebar, upload panel with drag-drop + paste, explicit evaluation scope/name, sample load/download. Dataset stats, report cards P/R/F1/Q/final with pending never0, run history + select2 compare, paper/item filters and details, answer options, gold matches/misses, structural issues, human rubric review form, judge button, progress polling, export. Responsive, keyboard labels/status, no innerHTML of user content. Show demo as manually authored format sample not a real model benchmark. Dataset explorer and methodology guide tabs recommended. Frontend reads above API contract.
