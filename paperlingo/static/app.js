"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = { session: null, config: null, judgeStatus: null, judgeStatusTimer: null, run: null, runs: [], selectedRuns: new Set(), reportPage: 0, datasetPage: 0, datasetTotal: 0, pollTimer: null, pollVersion: 0, toastTimer: null, datasetSequence: 0, review: null, isSample: false, skippingRunId: null };
const PAGE_SIZE = 10;
const pageLabels = { evaluate: msg("모델 평가"), history: msg("평가 기록"), dataset: msg("참조 데이터"), method: msg("평가 기준") };
const scopeLabels = { demo: msg("예시 데이터"), train: msg("학습 (이전 범위)"), evaluation: msg("평가"), development: msg("개발"), validation: msg("검증"), test: msg("최종 평가"), all: msg("전체"), custom: msg("직접 지정") };
const statusLabels = { awaiting_review: msg("검수 대기"), queued: msg("자동 검수 대기"), judging: msg("자동 검수 중"), complete: msg("완료"), needs_review: msg("재검토 필요"), error: msg("검수 오류"), pending: msg("검수 대기") };
const rubricLabels = { plausibility: msg("오답의 그럴듯함"), learning_alignment: msg("학습 목표 관련성"), distinctness: msg("오답 간 구별성"), no_cues: msg("불필요한 정답 단서 억제") };
const rubricDescriptions = { plausibility: msg("B1–B2 학습자가 혼동할 만한 오답인가요?"), learning_alignment: msg("목표 어휘·결합·문법 지식으로 구별하나요?"), distinctness: msg("세 오답이 구별되며 불필요하게 반복되지 않나요?"), no_cues: msg("길이·표기·형식만으로 정답이 드러나지 않나요?") };

function node(tag, className, text, raw = false) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) {
    if (raw) element.textContent = I18n.resolve(text, false);
    else I18n.setText(element, text);
  }
  return element;
}
function number(value) { return Number(value || 0).toLocaleString(I18n.locale); }
function percent(value, suffix = "%") { return value === null || value === undefined ? "—" : Number(value).toFixed(1) + suffix; }
function date(value) { const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleString(I18n.locale, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
function displayDomain(value) { return typeof value === "object" && value ? value.name || value.code || "" : value || ""; }
function tag(text, kind = "neutral") { return node("span", `tag ${kind}`, () => text); }
function statusTag(status) { return tag(statusLabels[status] || status, status === "complete" ? "success" : status === "error" ? "error" : "warning"); }
function judgeActive(run) { return ["queued", "judging"].includes(run?.status); }
function temporaryRun(run) { const report = run?.report || run; return run?.score_mode === "temporary" || report?.score_mode === "temporary" || Number(report?.counts?.quality_temporary || 0) > 0 || !!report?.papers?.some(paper => paper.items.some(item => item.quality?.source === "temporary")); }
function codeRun(run) { const report = run?.report || run; return report?.quality_policy_version === "code-basic-v1"; }
function referenceCount(paper) { return paper.reference_hidden ? paper.gold_count : (paper.gold_expressions || []).length; }
function missingCount(paper) { return paper.reference_hidden ? paper.missing_count : (paper.missing_expressions || []).length; }
function scoringRule(version) {
  if (version === "soft-rq-v4") return { version, label: "v4", formula: "100 × Rsoft × Qmatched", legacy: false, soft: true };
  if (version === "exact-rq-v3") return { version, label: "v3", formula: "100 × R × Qmatched", legacy: false };
  if (["exact-prq-v1", "exact-prq-v2"].includes(version)) return { version, label: version.endsWith("v1") ? "v1" : "v2", formula: "100 × P × R × Qmatched", legacy: true };
  return { version: version || msg("버전 정보 없음"), label: msg("이전 계산식"), formula: "100 × P × R × Qmatched", legacy: true };
}
function localJudge() { try { return ["localhost", "127.0.0.1", "::1", "[::1]"].includes(new URL(state.config?.judge?.base_url).hostname); } catch { return false; } }
function empty(title, description) { const box = node("div", "empty-state"); box.append(node("strong", "", () => title), node("p", "", () => description)); return box; }
function showMessage(selector, message, kind = "") { const element = $(selector); I18n.setText(element, message); element.className = `notice ${kind}`; element.hidden = !I18n.resolve(message); }
function toast(message) { clearTimeout(state.toastTimer); I18n.setText($("#toast"), () => message); $("#toast").hidden = false; state.toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 4500); }
function errorText(data, fallback) {
  const detail = data?.detail ?? data;
  if (typeof detail === "string") return detail;
  if (detail && Array.isArray(detail.errors)) return I18n.join([detail.message, ...detail.errors.map(item => typeof item === "string" ? item : item.message || JSON.stringify(item))].filter(Boolean), "\n");
  if (Array.isArray(detail)) return I18n.join(detail.map(item => item.msg || item.message || JSON.stringify(item)), "\n");
  return detail?.message || fallback;
}
function uiError(value) { const error = new Error(I18n.resolve(value)); error.uiMessage = value; return error; }
async function api(path, options = {}) {
  const headers = { "X-Requested-With": "PaperLingo", ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try { response = await fetch(path, { credentials: "same-origin", ...options, headers }); }
  catch { throw uiError(msg("서버에 연결할 수 없습니다. 연결 상태를 확인하고 다시 시도하세요.")); }
  let data;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) {
    if (response.status === 401 && !path.endsWith("/login")) openLogin();
    throw uiError(errorText(data, msg("요청을 처리하지 못했습니다. (HTTP {0})", response.status)));
  }
  return data;
}
const busyLabels = new WeakMap();
function setBusy(button, busy, label) {
  if (busy) {
    if (!busyLabels.has(button)) busyLabels.set(button, I18n.sourceText(button));
    button.disabled = true;
    button.replaceChildren(node("span", "busy-spin"), I18n.textNode(label));
  } else {
    button.disabled = false; I18n.setText(button, busyLabels.get(button) || label || ""); busyLabels.delete(button);
  }
}
function optionLabel(item, id) {
  const option = (Array.isArray(item.options) ? item.options : []).find(value => value && value.id === id);
  return option ? `${id} · ${option.text}` : (id ?? msg("없음"));
}
function explainQuality(item, quality) {
  const rubric = quality.rubric || {};
  const rubricComplete = Object.keys(rubricLabels).every(key => Number.isInteger(rubric[key]) && rubric[key] >= 0 && rubric[key] <= 2);
  const rubricTotal = rubricComplete ? Object.keys(rubricLabels).reduce((sum, key) => sum + rubric[key], 0) : null;
  const reasons = [];
  if (item.structure_valid === false) reasons.push(msg("입력 구조 오류로 문항 품질이 0점 처리되었습니다. 위의 구조 오류를 확인하세요."));
  else {
    if ((quality.source === "llm" && quality.review_recommended) || quality.option_assessments?.some(assessment => assessment.verdict === "uncertain")) reasons.push(msg("LLM이 보기의 정답 여부를 확정하지 못해 품질 0점으로 처리했습니다. 사람의 검수를 권장합니다."));
    const accepted = Array.isArray(quality.accepted_option_ids) ? quality.accepted_option_ids : [];
    if (!accepted.length) reasons.push(msg("정답으로 허용한 보기가 없습니다."));
    else if (accepted.length > 1) reasons.push(msg("정답으로 허용한 보기가 {0}개여서 단일 정답 조건을 충족하지 않습니다.", accepted.length));
    else if (accepted[0] !== item.answer_id) reasons.push(msg("허용한 정답 ({0})이 생성기 지정 정답 ({1})과 다릅니다.", optionLabel(item, accepted[0]), optionLabel(item, item.answer_id)));
    const gateReasons = { context_sufficient: msg("제공된 문맥이 충분하지 않다고 판정했습니다."), no_specialist_knowledge: msg("외부 전문 지식이 필요하다고 판정했습니다."), no_answer_leak: msg("정답을 직접 알려주는 표시나 설명이 있다고 판정했습니다.") };
    Object.entries(gateReasons).forEach(([key, reason]) => { if (quality.gates?.[key] === false) reasons.push(reason); });
    if (rubricComplete && rubricTotal === 0) reasons.push(msg("네 가지 품질 항목의 합계가 0점입니다."));
  }
  return { reasons, rubricComplete, rubricTotal, score: rubricComplete ? reasons.length ? 0 : rubricTotal / 8 * 100 : null };
}
function updateReviewPreview() {
  if (!state.review) return;
  const form = $("#review-form"), rubric = {}, gates = {};
  Object.keys(rubricLabels).forEach(key => { const value = form.elements[key]?.value; rubric[key] = value === "" || value === undefined ? null : Number(value); });
  ["context_sufficient", "no_specialist_knowledge", "no_answer_leak"].forEach(key => { gates[key] = form.elements[key].checked; });
  const accepted = $$("input[name=accepted_option]:checked", form).map(input => input.value);
  const result = explainQuality(state.review.item, { accepted_option_ids: accepted, rubric, gates });
  const box = $("#review-preview");
  box.className = `review-preview${result.score === 0 ? " zero" : result.score > 0 ? " positive" : ""}`;
  box.replaceChildren(node("strong", "review-preview-title", () => result.score === null ? msg("저장 예상 품질 · 검수 미완성") : msg("저장 예상 품질 · {0}", percent(result.score))));
  box.append(node("p", "", () => result.rubricComplete ? msg("품질 항목 합계 {0} / 8점{1}", result.rubricTotal, result.score === 0 && result.rubricTotal > 0 ? msg(" · 필수 통과 조건을 충족하지 않아 0%입니다.") : "") : msg("네 가지 품질 항목의 점수를 모두 선택하면 예상 점수를 표시합니다.")));
  if (result.reasons.length) {
    box.append(node("p", "preview-reason-heading", () => result.score === null ? msg("현재 판정에서 0점으로 처리되는 조건") : msg("0점 처리 이유")));
    const list = node("ul", "score-reasons"); result.reasons.forEach(reason => list.append(node("li", "", () => reason))); box.append(list);
  } else if (result.score !== null) box.append(node("p", "", () => msg("허용한 정답이 생성기 지정 정답과 일치하고, 필수 조건을 모두 충족합니다.")));
}

function navigate(page, updateHash = true) {
  if (!(page in pageLabels)) page = "evaluate";
  $$(".page").forEach(element => { element.hidden = element.id !== `page-${page}`; });
  $$(".nav-item").forEach(button => { const active = button.dataset.page === page; button.classList.toggle("active", active); if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current"); });
  I18n.setText($("#breadcrumb-current"), () => pageLabels[page]);
  if (updateHash && location.hash !== `#${page}`) history.replaceState(null, "", `#${page}`);
  if (state.config && page === "history") loadHistory();
  if (state.config && page === "dataset") loadDataset();
}
function openLogin() { if (!$("#login-dialog").open) $("#login-dialog").showModal(); }
function updateAccess() {
  const session = state.session || {};
  $$(".manage-only").forEach(button => { button.hidden = !session.can_manage; });
  $("#readonly-banner").hidden = !!session.can_manage;
  I18n.setText($("#access-label"), () => session.public_access ? msg("공개 워크스페이스") : session.can_manage ? session.auth_required ? msg("팀 관리자") : msg("로컬 워크스페이스") : msg("읽기 전용"));
  $("#auth-button").hidden = !session.auth_required;
  I18n.setText($("#auth-button"), () => session.authenticated && session.can_manage ? msg("로그아웃") : msg("로그인"));
  $("#login-close").hidden = session.auth_required && !session.public_read && !session.authenticated;
  $$("#evaluation-form input, #evaluation-form textarea, #evaluation-form select").forEach(input => { input.disabled = !session.can_manage; });
}
async function initialize() {
  try {
    state.session = await api("/api/session");
    updateAccess();
    if (state.session.auth_required && !state.session.authenticated && !state.session.public_read) { openLogin(); I18n.setText($("#connection-status"), () => msg("팀 로그인 필요")); return; }
    state.config = await api("/api/config");
    if (state.config.metrics_version) I18n.setText($("#protocol-version"), () => msg("평가 프로토콜 {0}", scoringRule(state.config.metrics_version).label));
    state.judgeStatus = { configured: !!state.config.judge?.configured, ready: false, message: msg("평가 모델의 연결 상태를 확인 중입니다.") };
    const summary = state.config.dataset;
    const evaluationStats = summary.split_stats.evaluation;
    I18n.setText($("#stat-papers"), () => number(evaluationStats.paper_count));
    I18n.setText($("#stat-expressions"), () => number(evaluationStats.expression_count));
    I18n.setText($("#stat-domains"), () => number(evaluationStats.domain_count));
    ["demo", "evaluation"].forEach(scope => {
      const value = summary.splits?.[scope];
      I18n.setText($("#dataset-size-" + scope), () => value === undefined ? "—" : number(Array.isArray(value) ? value.length : value));
    });
    I18n.setText($("#dataset-version"), () => `DATA ${String(summary.version || "v2").slice(0, 12)}`);
    const maxBytes = state.config.limits?.max_upload_bytes;
    if (maxBytes) I18n.setText($("#upload-limit"), () => msg("최대 {0} MB", Math.round(maxBytes / 1024 / 1024)));
    I18n.setText($("#connection-status"), () => msg("워크스페이스 연결됨"));
    $("#connection-dot").style.background = "";
    showMessage("#global-message", "");
    navigate(location.hash.slice(1) || "evaluate", false);
    refreshJudgeStatus();
  } catch (error) { showMessage("#global-message", (error.uiMessage || error.message), "error"); I18n.setText($("#connection-status"), () => msg("연결 확인 필요")); $("#connection-dot").style.background = "#c0856e"; }
}
async function refreshJudgeStatus() {
  clearTimeout(state.judgeStatusTimer);
  if (!state.config) return;
  try {
    state.judgeStatus = await api("/api/judge/status");
  } catch {
    state.judgeStatus = { configured: !!state.config.judge?.configured, ready: false, message: msg("평가 모델의 연결 상태를 확인할 수 없습니다.") };
  }
  const status = state.judgeStatus;
  const label = $("#judge-status-label");
  if (label) {
    I18n.setText(label, () => status.mode === "code" ? msg("코드 검사 · 4개 항목") : status.ready ? msg("{0} 연결됨 · 검수 가능", localJudge() ? msg("로컬 LLM") : "LLM") : status.configured ? msg("LLM 준비 대기 · 사람 검수 가능") : msg("LLM 미설정 · 사람 검수 가능"));
    label.className = `tag ${status.ready ? "success" : "neutral"}`;
    I18n.setAttribute(label, "title", () => status.message || "");
  }
  if (state.run) renderReport();
  if (status.configured && !status.ready) state.judgeStatusTimer = setTimeout(refreshJudgeStatus, 10000);
}
async function readFile(file) {
  if (!state.session?.can_manage || !file) return;
  const limit = state.config?.limits?.max_upload_bytes || 10 * 1024 * 1024;
  if (file.size > limit) { showMessage("#upload-errors", msg("파일 크기가 업로드 한도를 초과합니다."), "error"); return; }
  try {
    $("#prediction-text").value = await file.text();
    I18n.setText($("#file-label"), () => `${file.name} · ${(file.size / 1024).toFixed(1)} KB`);
    state.isSample = false; $("#demo-notice").hidden = true;
    if (!$("#run-name").value) $("#run-name").value = file.name.replace(/\.(jsonl|json)$/i, "").slice(0, 120);
    showMessage("#upload-errors", "");
  } catch { showMessage("#upload-errors", msg("파일을 읽지 못했습니다. 다시 선택해주세요."), "error"); }
}
async function loadSample() {
  const button = $("#load-sample"); setBusy(button, true, msg("불러오는 중"));
  try {
    const sample = await api("/api/sample");
    $("#prediction-text").value = sample.predictions.map(prediction => JSON.stringify(prediction)).join("\n");
    $("#run-name").value = msg("입력 형식 예제 · 참조 표현 전체");
    const questionCount = sample.predictions.reduce((sum, paper) => sum + paper.questions.length, 0);
    I18n.setText($("#file-label"), () => msg("참조 표현을 모두 담은 형식 예제 · {0}편 / {1}문항", sample.predictions.length, questionCount));
    state.isSample = true; $("#demo-notice").hidden = false;
    showMessage("#upload-errors", ""); toast(msg("예제를 불러왔습니다. 자동 평가 시작을 누르세요."));
  } catch (error) { showMessage("#upload-errors", (error.uiMessage || error.message), "error"); }
  finally { setBusy(button, false); }
}
async function submitEvaluation(event) {
  event.preventDefault();
  const predictions = $("#prediction-text").value.trim();
  if (!predictions) { showMessage("#upload-errors", msg("생성 결과 파일을 선택하거나 JSON/JSONL을 붙여넣어주세요."), "error"); $("#prediction-text").focus(); return; }
  const payload = { name: $("#run-name").value.trim(), predictions };
  const button = $("#evaluate-button"); setBusy(button, true, msg("표현 채점 중")); showMessage("#upload-errors", "");
  try {
    const run = await api("/api/runs", { method: "POST", body: JSON.stringify(payload) });
    state.run = run; state.reportPage = 0;
    $("#report-search").value = ""; $("#report-filter").value = "all";
    renderReport(); schedulePoll();
    $("#report-section").scrollIntoView({ behavior: "smooth", block: "start" });
    toast(codeRun(run) ? msg("표현 채점과 네 가지 코드 검사가 완료되었습니다.") : judgeActive(run) ? msg("표현 채점이 완료되었습니다. 보기 품질은 자동으로 검수합니다.") : msg("평가를 생성했습니다. 결과와 자동 검수 상태를 확인하세요."));
  } catch (error) { showMessage("#upload-errors", (error.uiMessage || error.message), "error"); }
  finally { setBusy(button, false); }
}

function metricCard(label, value, detail, featured = false, pending = false, suffix = "%") {
  const card = node("div", `metric-card${featured ? " featured" : ""}`);
  card.append(node("div", "metric-label", () => label));
  const numeral = node("div", `metric-number${value == null ? " pending" : ""}`);
  if (value == null) I18n.setText(numeral, () => pending ? msg("검수 대기") : msg("해당 없음"));
  else numeral.append(document.createTextNode(Number(value).toFixed(1)), node("small", "", () => suffix));
  card.append(numeral, node("div", "metric-detail", () => detail));
  const bar = node("div", "metric-bar"); const fill = node("span"); fill.style.width = `${Math.max(0, Math.min(100, Number(value || 0)))}%`; bar.append(fill); card.append(bar); return card;
}
function secondaryMetric(label, value) { const element = node("div", "secondary-metric"); element.append(document.createTextNode(label), node("strong", "", () => value)); return element; }
function renderReport() {
  const run = state.run;
  if (!run) return;
  const report = run.report, metrics = report.metrics, counts = report.counts;
  const rule = scoringRule(report.metrics_version), code = codeRun(run);
  const unresolved = (counts.quality_pending || 0) + (counts.quality_error || 0) + (counts.needs_review || 0);
  const temporary = temporaryRun(run), active = judgeActive(run), temporaryCount = counts.quality_temporary || 0;
  const uncertainCount = report.papers.reduce((sum, paper) => sum + paper.items.filter(item => item.quality?.source === "llm" && item.quality.option_assessments?.some(assessment => assessment.verdict === "uncertain")).length, 0);
  const hasValidItems = report.papers.some(paper => paper.items.some(item => item.structure_valid));
  const needsJudge = unresolved > 0 || temporary;
  $("#report-section").hidden = false;
  $("#report-title").textContent = run.name;
  I18n.setText($("#report-meta"), () => msg("{0} · {1} {2}편 · {3} · {4}{5} · {6}{7}", date(run.created_at), scopeLabels[report.scope.name] || report.scope.name, number(report.scope.paper_count), statusLabels[run.status] || run.status, rule.legacy ? msg("이전 기준 · ") : "", rule.version, report.quality_policy_version || msg("이전 검수 기준"), temporary ? msg(" · 임시 점수") : ""));
  $("#download-json").href = `/api/runs/${encodeURIComponent(run.id)}/export?format=json`;
  $("#download-csv").href = `/api/runs/${encodeURIComponent(run.id)}/export?format=csv`;
  $("#temporary-banner").hidden = !temporary;
  I18n.setText($("#skip-judge-help"), () => msg("구조가 유효한 모든 문항의 Q를 100%로 처리합니다(기존 검수 포함). 이 기록의 최종 점수는 {0}이며 검증된 성능이 아닙니다.", rule.formula));
  if (temporary) I18n.setText($("#temporary-count"), () => msg("{0}개 문항의 품질을 검수 없이 100%로 임시 처리했습니다. 구조 오류는 0점이며 최종 점수는 {1}로 계산합니다({2}). 검증된 성능이 아니므로 일반 평가와 비교할 수 없습니다.", number(temporaryCount), rule.formula, rule.version));
  const cards = $("#metric-cards"); cards.replaceChildren(
    rule.soft
      ? metricCard(msg("Recall · 부분 점수 포함"), metrics.recall_pct, msg("정확 {0} + 부분 {1} = {2}점 / {3}개 실제 참조", number(counts.exact_matched), number(counts.partial_matched), number(counts.credited_matches), number(counts.gold)))
      : metricCard(msg("Recall · 표현 재현율"), metrics.recall_pct, msg("{0}개 일치 / {1}개 실제 참조", number(counts.matched), number(counts.gold))),
    rule.soft
      ? metricCard(msg("Precision · 보조 지표"), metrics.precision_pct, msg("{0}점 / {1}개 생성", number(counts.credited_matches), number(counts.predicted)))
      : metricCard(rule.legacy ? msg("Precision · 선정 정확도") : msg("Precision · 보조 지표"), metrics.precision_pct, msg("{0}개 일치 / {1}개 생성", number(counts.matched), number(counts.predicted))),
    metricCard(msg("F1 · 보조 지표"), metrics.f1_pct, msg("Precision과 Recall의 조화평균")),
    metricCard(code ? msg("Q · 일치 문항 통과율") : temporary ? msg("Q · 일치 문항 품질 (임시)") : msg("Q · 일치 문항 품질"), metrics.quality_matched_pct, code ? msg("참조 라벨과 일치한 문항 중 검사 통과 비율") : temporary ? msg("검수 없이 만점 처리한 문항 포함") : msg("참조 라벨과 일치한 문항의 평균"), false, unresolved > 0 && counts.matched > 0),
    metricCard(temporary ? msg("최종 점수 · 임시") : rule.legacy ? msg("최종 점수 · 이전 기준") : msg("최종 점수"), metrics.final_score, msg("{0}{1}", temporary ? msg("검증 전 임시값 · ") : "", rule.formula), true, unresolved > 0, "/ 100")
  );
  $("#report-secondary").replaceChildren(
    secondaryMetric(code ? msg("전체 문항 통과율") : msg("전체 문항 품질"), percent(metrics.quality_all_pct)),
    secondaryMetric(code ? msg("원문 일치 유일률") : msg("단일 정답률"), percent(code ? metrics.source_unique_answer_rate_pct : metrics.unique_answer_rate_pct)),
    secondaryMetric(msg("구조 유효율"), percent(metrics.structural_validity_pct)),
    secondaryMetric(msg("논문 제출률"), percent(metrics.coverage_pct)),
    secondaryMetric(msg("빈 라벨 미생성 정확도"), percent(metrics.empty_gold_abstention_pct)),
    secondaryMetric(code ? msg("코드 검사 완료") : msg("품질 처리"), msg("{0} / {1}개", number(counts.quality_complete), number(counts.predicted)))
  );
  if (temporaryCount) $("#report-secondary").append(secondaryMetric(msg("임시 만점 처리"), msg("{0}개", number(temporaryCount))));
  if (counts.review_recommended) $("#report-secondary").append(secondaryMetric(msg("사람 검수 권장"), msg("{0}개", number(counts.review_recommended))));
  const notices = [];
  if (code) notices.push(msg("코드 검사 결과입니다. 유일한 정답은 원문 빈칸과 문자열이 일치하는 보기가 하나라는 뜻입니다. 문맥 충분성·의미적 복수 정답·오답의 교육적 품질은 검사하지 않았습니다."));
  else if (state.config?.review_mode === "code") notices.push(msg("이 기록은 이전 검수 기준을 유지합니다. 네 가지 코드 검사 점수는 같은 입력으로 새 평가를 제출하면 확인할 수 있습니다."));
  if (rule.legacy) notices.push(msg("이 기록은 이전 기준 {0}({1})로 계산한 점수를 유지합니다. 새 평가의 v3 기준과 직접 비교할 수 없습니다.", rule.version, rule.formula));
  if (run.is_demo) notices.push(msg("형식 예제 · 참조 라벨을 참고해 직접 작성한 예제입니다. 실제 생성 모델의 기준 성능이 아닙니다."));
  if (report.papers.some(paper => paper.items.some(item => item.quality?.source === "llm"))) notices.push(msg("LLM 판정에는 오류가 있을 수 있습니다. 기준 성능 비교 전 문항별 근거와 사람의 표본 검수를 확인하세요."));
  $("#judge-progress").hidden = !active;
  if (active) {
    const total = Number(run.judge_meta?.total ?? counts.predicted), processed = Number(run.judge_meta?.processed || 0);
    I18n.setText($("#judge-progress-label"), () => msg("{0} · {1} / {2}개", run.status === "queued" ? msg("자동 검수 대기") : msg("자동 검수 진행 중"), number(processed), number(total)));
    $("#judge-progress-bar").max = Math.max(total, 1);
    $("#judge-progress-bar").value = Math.min(processed, total);
    I18n.setText($("#judge-progress-help"), () => run.status === "queued" ? msg("평가 모델이 준비되면 순서대로 시작합니다. 모든 문항을 자동으로 처리합니다.") : msg("추가 실행 없이 남은 문항까지 계속 검수합니다. 결과가 자동으로 갱신됩니다."));
  } else if (unresolved && state.config?.review_mode !== "code") {
    notices.push(msg("보기 검수가 {0}개 남았습니다. 오류 문항을 다시 검수하거나 임시 품질 만점으로 처리할 수 있습니다.", number(unresolved)));
    if (counts.quality_error) notices.push(msg("평가 연결 또는 응답 오류 {0}개는 재시도할 수 있습니다.", number(counts.quality_error)));
    if (counts.needs_review) notices.push(msg("기존 미완료 판정 {0}개는 LLM 또는 사람이 다시 검수할 수 있습니다.", number(counts.needs_review)));
    if (state.config?.judge?.configured && state.judgeStatus?.ready === false) notices.push(msg("LLM 연결 준비 대기 · {0} 준비 상태를 자동 확인합니다.", state.judgeStatus.message || msg("평가 모델이 아직 연결되지 않았습니다.")));
    else if (!state.config?.judge?.configured && state.session?.can_manage) notices.push(msg("평가 모델이 설정되지 않았습니다. 사람 검수 또는 임시 품질 만점 처리를 사용할 수 있습니다."));
  }
  if (uncertainCount) notices.push(msg("판단이 불확실한 {0}개 문항은 품질 0점으로 처리했으며 사람의 검수를 권장합니다.", number(uncertainCount)));
  if (counts.missing_papers) notices.push(msg("평가 대상 중 {0}편이 미제출입니다. 해당 참조 표현도 Recall에 포함됩니다.", number(counts.missing_papers)));
  showMessage("#report-notice", I18n.join(notices, "\n"), counts.quality_error ? "warning" : "");
  const judgeButton = $("#judge-button");
  judgeButton.hidden = code || state.config?.review_mode === "code" || !state.session?.can_manage || (!active && !needsJudge);
  judgeButton.disabled = !state.config?.judge?.configured || state.judgeStatus?.ready === false || active || state.skippingRunId === run.id;
  I18n.setText(judgeButton, () => run.status === "queued" ? msg("자동 검수 대기 중") : run.status === "judging" ? msg("자동 검수 진행 중…") : temporary ? msg("임시 점수 대신 LLM 검수") : counts.quality_error ? msg("LLM 검수 다시 시도") : msg("LLM 검수 시작"));
  I18n.setAttribute(judgeButton, "title", () => state.judgeStatus?.ready === false ? state.judgeStatus.message || msg("평가 모델의 연결을 기다리는 중입니다.") : msg("미완료 문항과 임시 점수 문항을 검수합니다. 완료된 실제 검수 결과는 유지합니다."));
  $("#skip-judge-actions").hidden = code || !state.session?.can_manage || !hasValidItems;
  const skipButton = $("#skip-judge-button");
  skipButton.disabled = state.skippingRunId === run.id;
  I18n.setText(skipButton, () => state.skippingRunId === run.id ? msg("임시 점수 적용 중…") : msg("검수 스킵 · 품질 만점 (임시)"));
  renderPapers();
}
function itemMatchesFilter(item, filter) {
  if (filter === "pass" || filter === "fail") return item.quality?.source === "code" && item.quality.result === filter;
  if (filter === "matched") return item.matched;
  if (filter === "unmatched") return !item.matched;
  if (filter === "pending") return item.quality?.status !== "complete";
  if (filter === "temporary") return item.quality?.source === "temporary";
  if (filter === "review_recommended") return !!item.quality?.review_recommended;
  if (filter === "invalid") return !item.structure_valid;
  return true;
}
function renderPapers() {
  if (!state.run) return;
  const oldOpen = new Set($$("#paper-results > details[open]").map(element => element.dataset.paperId));
  const query = $("#report-search").value.trim().toLowerCase(), filter = $("#report-filter").value;
  const papers = state.run.report.papers.filter(paper => {
    const searchMatch = !query || `${paper.paper_id} ${paper.paper_name} ${(paper.items || []).map(item => item.expression_text).join(" ")} ${(paper.gold_expressions || []).join(" ")}`.toLowerCase().includes(query);
    if (!searchMatch) return false;
    if (filter === "missing") return !paper.submitted || missingCount(paper);
    if (filter !== "all") return paper.items.some(item => itemMatchesFilter(item, filter));
    return true;
  });
  const pages = Math.max(1, Math.ceil(papers.length / PAGE_SIZE));
  state.reportPage = Math.min(state.reportPage, pages - 1);
  I18n.setText($("#detail-count"), () => msg("{0}편", number(papers.length)));
  const container = $("#paper-results"); container.replaceChildren();
  if (!papers.length) container.append(empty(msg("조건에 맞는 결과가 없습니다"), msg("검색어 또는 필터를 바꿔보세요.")));
  papers.slice(state.reportPage * PAGE_SIZE, (state.reportPage + 1) * PAGE_SIZE).forEach((paper, index) => {
    const details = node("details", "paper-result"); details.dataset.paperId = paper.paper_id;
    details.open = oldOpen.has(paper.paper_id) || (oldOpen.size === 0 && papers.length <= 3 && index === 0);
    const summary = node("summary"), copy = node("div", "paper-summary-copy");
    copy.append(node("div", "paper-summary-title", () => paper.paper_name || paper.paper_id, true));
    const meta = node("div", "paper-summary-meta"); meta.append(node("span", "", () => paper.paper_id), node("span", "", () => displayDomain(paper.domain)), node("span", "", () => msg("생성 {0} / 참조 {1}", paper.items.length, referenceCount(paper)))); copy.append(meta);
    const tags = node("div", "paper-summary-tags");
    if (!paper.submitted) tags.append(tag(msg("제출 누락"), "error"));
    else if (!paper.items.length) tags.append(tag(msg("생성 표현 없음"), "neutral"));
    else tags.append(tag(msg("{0}개 일치", paper.items.filter(item => item.matched).length), "success"));
    if (missingCount(paper)) tags.append(tag(msg("{0}개 미일치", missingCount(paper)), "warning"));
    summary.append(copy, tags); details.append(summary);
    const body = node("div", "paper-body"), goldBlock = node("div", "gold-block");
    if (paper.reference_hidden) {
      goldBlock.append(node("div", "label-caption", () => msg("REFERENCE EXPRESSIONS · 비공개")), node("p", "muted", () => msg("평가용 선정 표현은 공개하지 않습니다. 제출한 표현의 일치 여부와 점수는 아래에서 확인할 수 있습니다.")));
    } else {
      goldBlock.append(node("div", "label-caption", () => msg("REFERENCE EXPRESSIONS · 참조 표현")));
      const chips = node("div", "chip-list");
      (paper.gold_expressions || []).forEach(expression => { const missing = (paper.missing_expressions || []).includes(expression); const chip = node("span", `expression-chip${missing ? " missing" : ""}`, () => expression, true); I18n.setAttribute(chip, "title", () => missing ? msg("생성 결과와 일치하지 않은 참조 표현") : msg("생성 결과와 일치")); chips.append(chip); });
      if (!paper.gold_expressions?.length) chips.append(node("span", "muted", () => msg("참조 표현 없음 · 명시적인 빈 출력을 확인합니다.")));
      goldBlock.append(chips);
    }
    body.append(goldBlock);
    const source = node("details", "abstract-details"); source.append(node("summary", "", () => msg("논문 원문 확인")), node("div", "abstract-text", () => paper.abstract, true)); body.append(source);
    const visibleItems = paper.items.filter(item => itemMatchesFilter(item, filter));
    visibleItems.forEach(item => body.append(renderQuestion(paper, item)));
    if (!paper.items.length) body.append(node("p", "muted", () => paper.submitted ? msg("이 논문은 빈 questions 배열로 제출되었습니다.") : msg("이 논문의 생성 결과가 제출되지 않았습니다.")));
    details.append(body); container.append(details);
  });
  I18n.setText($("#report-page-label"), () => `${state.reportPage + 1} / ${pages}`);
  $("#report-prev").disabled = state.reportPage === 0;
  $("#report-next").disabled = state.reportPage >= pages - 1;
}
function contextNode(context) {
  const block = node("blockquote", "question-context");
  if (!context) { I18n.setText(block, msg("문맥을 구성할 수 없습니다. 학습 표현이 원문에 있고 정답 보기가 표현에 포함되는지 확인하세요.")); return block; }
  const parts = String(context || "").split("[BLANK]");
  parts.forEach((part, index) => { if (index) block.append(node("span", "blank-marker", () => "_______")); block.append(document.createTextNode(part)); });
  return block;
}
function renderQuestion(paper, item) {
  const card = node("article", "question-card");
  const heading = node("div", "question-heading"), title = node("div");
  const questionLabel = item.input_format === "compact-v1" ? msg("문항 {0}", item.inference.question_position) : `${item.question_id}${item.expression_type ? ` · ${item.expression_type}` : ""}`;
  title.append(node("span", "question-ident", () => questionLabel), node("h3", "question-title", () => item.expression_text, true));
  const badges = node("div", "question-badges"); const partial = item.matched && item.match_credit != null && item.match_credit < 1;
  badges.append(tag(partial ? msg("부분 일치 {0}", percent(item.match_credit * 100)) : item.matched ? msg("라벨 일치") : msg("라벨 불일치"), item.matched && !partial ? "success" : "warning"));
  if (partial && item.matched_expression) badges.append(tag(msg("참조: {0}", item.matched_expression)));
  if (!item.structure_valid) badges.append(tag(msg("구조 오류"), "error"));
  const quality = item.quality || { status: "pending" };
  badges.append(quality.source === "code" ? tag(quality.result === "pass" ? msg("검사 통과") : msg("검사 실패"), quality.result === "pass" ? "success" : "error") : quality.source === "temporary" ? tag(msg("품질 {0} · 임시", percent(quality.q == null ? null : quality.q * 100)), "temporary") : quality.status === "complete" ? tag(msg("품질 {0}", percent(quality.q == null ? null : quality.q * 100)), quality.q > 0 ? "success" : "warning") : statusTag(quality.status));
  if (quality.review_recommended) badges.append(tag(msg("사람 검수 권장"), "warning"));
  heading.append(title, badges); card.append(heading, contextNode(item.context));
  const options = node("div", "options-grid");
  (Array.isArray(item.options) ? item.options : []).forEach(option => {
    if (!option || typeof option !== "object" || Array.isArray(option)) { options.append(node("div", "option", () => msg("잘못된 보기 형식 · 구조 오류를 확인하세요."))); return; }
    const optionNode = node("div", `option${option.id === item.answer_id ? " correct" : ""}`);
    optionNode.append(node("span", "option-id", () => option.id), node("span", "option-text", () => option.text, true));
    if (option.id === item.answer_id) optionNode.append(node("span", "answer-tag", () => msg("생성기 정답"))); options.append(optionNode);
  }); card.append(options);
  if (quality.source === "temporary") card.append(node("p", "temporary-item-note", () => msg("검수 스킵으로 품질을 임시 만점 처리했습니다. 보기의 적절성이 검증된 결과가 아닙니다.")));
  if (item.issues?.length) { const issues = node("div", "issue-list"); item.issues.forEach(issue => issues.append(node("p", "", () => msg("{0} · {1}", issue.code, msg(issue.message))))); card.append(issues); }
  if (quality.source !== "code" && quality.status === "complete" && quality.q === 0) {
    const explanation = explainQuality(item, quality), box = node("div", "zero-quality-reason");
    box.append(node("strong", "", () => msg("품질 0% · 처리 이유")));
    const list = node("ul", "score-reasons");
    const reasons = explanation.reasons.length ? explanation.reasons : [msg("필수 통과 조건 또는 품질 항목의 판정에 따라 0점 처리되었습니다. 검수 근거를 확인하세요.")];
    reasons.forEach(reason => list.append(node("li", "", () => reason))); box.append(list);
    if (explanation.rubricTotal > 0) box.append(node("p", "", () => msg("품질 항목 합계가 {0} / 8점이어도 필수 통과 조건을 충족해야 득점합니다.", explanation.rubricTotal)));
    card.append(box);
  }
  if (quality.source === "code") {
    const checks = node("ul", "code-check-list");
    (quality.checks || []).forEach(check => {
      const row = node("li", check.passed ? "check-pass" : "check-fail");
      const label = node("div", "code-check-label");
      label.append(node("strong", "", () => check.label), tag(check.passed ? msg("통과") : msg("실패"), check.passed ? "success" : "error"));
      row.append(label, node("p", "", () => check.message)); checks.append(row);
    });
    card.append(checks);
  }
  if (quality.source !== "code" && (quality.source || quality.rationale || quality.option_assessments?.length)) {
    const detail = node("details", "quality-detail");
    detail.append(node("summary", "", () => quality.source === "temporary" ? msg("임시 처리 내역 · 검수 생략") : msg("검수 근거{0}", quality.source === "human" ? msg(" · 사람 / {0}", quality.reviewer || "") : quality.source === "llm" ? " · LLM" : quality.source === "structural" ? msg(" · 구조 검증") : "")));
    if (quality.rationale) detail.append(node("p", "quality-reason", () => quality.rationale, ["human", "llm"].includes(quality.source)));
    (quality.option_assessments || []).forEach(assessment => detail.append(node("p", "quality-reason", () => msg("{0} · {1} — {2}", assessment.id, { acceptable: msg("허용 가능"), incorrect: msg("오답"), uncertain: msg("불확실") }[assessment.verdict] || assessment.verdict, assessment.reason || ""))));
    if (quality.rubric) { const scores = node("div", "rubric-summary"); Object.entries(rubricLabels).forEach(([key, label]) => scores.append(node("span", "", () => `${label} ${quality.rubric[key]} / 2`))); detail.append(scores); }
    card.append(detail);
  }
  const footer = node("div", "question-footer"); footer.append(node("p", "", () => item.learning_point || msg("선정한 전체 표현을 기준으로 라벨 일치 여부를 계산합니다."), !!item.learning_point));
  if (state.session?.can_manage && quality.source !== "code") {
    const review = node("button", "button secondary small", () => quality.source === "human" ? msg("검수 수정") : msg("사람 검수")); review.type = "button"; review.disabled = !item.structure_valid || judgeActive(state.run);
    I18n.setAttribute(review, "title", () => !item.structure_valid ? msg("구조 오류는 입력을 수정하여 새 평가를 제출하세요.") : judgeActive(state.run) ? msg("자동 검수가 끝나거나 검수를 스킵한 뒤 수정할 수 있습니다.") : msg("동일한 기준으로 문항 품질을 직접 검수합니다."));
    review.addEventListener("click", () => openReview(paper, item)); footer.append(review);
  }
  card.append(footer); return card;
}
async function loadRun(id) {
  try {
    state.run = await api(`/api/runs/${encodeURIComponent(id)}`); state.reportPage = 0;
    $("#report-search").value = ""; $("#report-filter").value = "all";
    navigate("evaluate"); renderReport(); schedulePoll();
    $("#report-section").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { toast((error.uiMessage || error.message)); }
}
function schedulePoll() {
  clearTimeout(state.pollTimer);
  const version = ++state.pollVersion;
  if (!judgeActive(state.run)) return;
  const id = state.run.id;
  state.pollTimer = setTimeout(async () => {
    try {
      const run = await api(`/api/runs/${encodeURIComponent(id)}`);
      if (state.run?.id !== id || state.pollVersion !== version) return;
      state.run = run; renderReport();
      if (!judgeActive(run)) { toast(temporaryRun(run) ? msg("검수를 스킵한 임시 점수를 표시합니다.") : run.report.counts.quality_error ? msg("자동 검수가 끝났습니다. 오류 문항은 다시 시도할 수 있습니다.") : msg("자동 검수가 완료되었습니다. 문항별 결과를 확인하세요.")); if (run.report.counts.quality_error) refreshJudgeStatus(); }
    } catch (error) { if (state.pollVersion === version) showMessage("#report-notice", I18n.join([(error.uiMessage || error.message), msg(" 잠시 후 다시 상태를 확인합니다.")]), "warning"); }
    if (state.pollVersion === version) schedulePoll();
  }, 2500);
}
async function startJudge() {
  if (!state.run || !state.session?.can_manage) return;
  const button = $("#judge-button"); setBusy(button, true, msg("검수 시작 중"));
  try {
    await api(`/api/runs/${encodeURIComponent(state.run.id)}/judge`, { method: "POST", body: "{}" });
    state.run = await api(`/api/runs/${encodeURIComponent(state.run.id)}`); renderReport(); schedulePoll();
  } catch (error) { setBusy(button, false); showMessage("#report-notice", (error.uiMessage || error.message), "error"); toast((error.uiMessage || error.message)); refreshJudgeStatus(); }
}
async function skipJudge() {
  if (!state.run || !state.session?.can_manage || state.skippingRunId) return;
  const id = state.run.id;
  state.skippingRunId = id; clearTimeout(state.pollTimer); state.pollVersion++;
  renderReport();
  try {
    const run = await api(`/api/runs/${encodeURIComponent(id)}/skip-judge`, { method: "POST", body: "{}" });
    if (state.run?.id === id) state.run = run;
    state.selectedRuns.delete(id);
    $("#comparison").hidden = true;
    toast(msg("구조가 유효한 문항의 품질을 100%로 임시 처리했습니다."));
  } catch (error) { toast((error.uiMessage || error.message)); if (state.run?.id === id) showMessage("#report-notice", (error.uiMessage || error.message), "error"); }
  finally { state.skippingRunId = null; if (state.run?.id === id) { renderReport(); schedulePoll(); } }
}

async function loadHistory() {
  const container = $("#run-list");
  $("#comparison").hidden = true;
  try {
    const result = await api("/api/runs"); state.runs = result.items || [];
    state.selectedRuns = new Set([...state.selectedRuns].filter(id => state.runs.some(run => run.id === id && !temporaryRun(run))));
    container.replaceChildren();
    if (!state.runs.length) { container.append(empty(msg("아직 평가 기록이 없습니다"), msg("모델 평가에서 예제를 불러와 첫 평가를 시작해보세요."))); $("#compare-button").disabled = true; return; }
    const table = node("table"), head = node("thead"), row = node("tr");
    [msg("선택"), msg("평가 이름"), msg("대상"), msg("상태"), "Recall", msg("보기 검사 / 품질"), msg("최종 점수")].forEach(label => row.append(node("th", "", () => label))); head.append(row); table.append(head);
    const body = node("tbody");
    state.runs.forEach(run => {
      const temporary = temporaryRun(run);
      const tr = node("tr"), checkCell = node("td"), checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = state.selectedRuns.has(run.id); checkbox.disabled = temporary; I18n.setAttribute(checkbox, "title", () => temporary ? msg("임시 품질 만점 기록은 성능 비교에 사용할 수 없습니다.") : msg("비교할 평가를 선택하세요.")); I18n.setAttribute(checkbox, "aria-label", () => msg("{0}{1}", run.name, temporary ? msg(" 임시 점수 · 비교 불가") : msg(" 비교 선택")));
      checkbox.addEventListener("change", () => { if (checkbox.checked && state.selectedRuns.size >= 2) { checkbox.checked = false; toast(msg("비교할 평가를 최대 2개 선택하세요.")); return; } if (checkbox.checked) state.selectedRuns.add(run.id); else state.selectedRuns.delete(run.id); $("#compare-button").disabled = state.selectedRuns.size !== 2; });
      checkCell.append(checkbox); tr.append(checkCell);
      const nameCell = node("td"), button = node("button", "run-name-button", () => run.name, true); button.addEventListener("click", () => loadRun(run.id)); nameCell.append(button);
      if (codeRun(run)) nameCell.append(document.createTextNode(" "), tag(msg("코드 검사")));
      if (run.is_demo) nameCell.append(document.createTextNode(" "), tag(msg("형식 예제")));
      if (temporary) nameCell.append(document.createTextNode(" "), tag(msg("임시 · 검수 스킵"), "temporary"));
      const rule = scoringRule(run.metrics_version);
      if (rule.legacy) nameCell.append(document.createTextNode(" "), tag(msg("이전 기준 {0}", rule.label)));
      nameCell.append(node("div", "run-date", () => `${date(run.created_at)} · ${rule.version}`)); tr.append(nameCell);
      const scope = typeof run.scope === "object" ? run.scope?.name : run.scope;
      tr.append(node("td", "", () => scopeLabels[scope] || scope || "—"));
      const status = node("td"); status.append(temporary && !judgeActive(run) ? tag(msg("임시 점수"), "temporary") : statusTag(run.status)); tr.append(status);
      tr.append(node("td", "table-number", () => percent(run.metrics?.recall_pct)), node("td", "table-number", () => percent(run.metrics?.quality_matched_pct)), node("td", "table-number", () => percent(run.metrics?.final_score, ""))); body.append(tr);
    }); table.append(body); container.append(table); $("#compare-button").disabled = state.selectedRuns.size !== 2;
  } catch (error) { container.replaceChildren(empty(msg("평가 기록을 불러오지 못했습니다"), (error.uiMessage || error.message))); }
}
async function compareRuns() {
  if (state.selectedRuns.size !== 2) return;
  const button = $("#compare-button"); setBusy(button, true, msg("비교 준비 중"));
  try {
    const results = await Promise.all([...state.selectedRuns].map(id => api(`/api/runs/${encodeURIComponent(id)}`)));
    if (results.some(temporaryRun)) {
      showMessage("#comparison-warning", msg("임시 품질 만점 기록이 포함되어 있어 성능을 비교할 수 없습니다. 해당 기록을 실제 LLM 또는 사람이 검수한 뒤 비교하세요."), "warning");
      $("#comparison-table").replaceChildren(tag(msg("비교 제외 · 임시 점수"), "temporary"));
      $("#comparison").hidden = false;
      return;
    }
    const [left, right] = results.map(run => run.report);
    const sameScope = JSON.stringify([...left.scope.paper_ids].sort()) === JSON.stringify([...right.scope.paper_ids].sort());
    const comparable = sameScope && left.dataset_version === right.dataset_version && left.metrics_version === right.metrics_version && left.quality_policy_version === right.quality_policy_version;
    const warnings = [];
    if (!comparable) warnings.push(msg("평가 데이터 버전, 지표·검수 규칙 버전 또는 대상 논문이 서로 다릅니다. 아래 수치는 참고용이며 동일 조건의 성능 비교로 해석할 수 없습니다."));
    if (results.some(run => run.is_demo)) warnings.push(msg("직접 작성한 형식 예제가 포함되어 있습니다. 실제 생성 모델의 성능 비교가 아닙니다."));
    if (results.some(run => run.report.metrics.final_score == null)) warnings.push(msg("검수가 미완료되었거나 최종 점수를 정의할 수 없는 평가가 포함되어 있습니다."));
    showMessage("#comparison-warning", I18n.join(warnings, "\n"), "warning");
    const table = node("table"), head = node("thead"), header = node("tr"); header.append(node("th", "", () => msg("비교 항목"))); results.forEach(run => header.append(node("th", "", () => run.name, true))); head.append(header); table.append(head);
    const rows = [[msg("평가 대상"), report => msg("{0} · {1}편", scopeLabels[report.scope.name] || report.scope.name, report.scope.paper_count)], [msg("데이터 버전"), report => String(report.dataset_version).slice(0, 12)], [msg("채점 기준 버전"), report => scoringRule(report.metrics_version).version], [msg("검수 기준"), report => report.quality_policy_version || msg("이전 기준")], [msg("최종 점수 계산식"), report => scoringRule(report.metrics_version).formula], ["Recall", report => percent(report.metrics.recall_pct)], [msg("일치 문항 통과율 / 품질"), report => percent(report.metrics.quality_matched_pct)], ["Precision", report => percent(report.metrics.precision_pct)], [msg("F1 · 보조 지표"), report => percent(report.metrics.f1_pct)], [msg("전체 문항 통과율 / 품질"), report => percent(report.metrics.quality_all_pct)], [msg("원문 일치 유일률 (코드)"), report => percent(report.metrics.source_unique_answer_rate_pct)], [msg("단일 정답률 (의미 검수)"), report => percent(report.metrics.unique_answer_rate_pct)], [msg("논문 제출률"), report => percent(report.metrics.coverage_pct)], [msg("최종 점수"), report => percent(report.metrics.final_score, " / 100")]];
    const body = node("tbody"); rows.forEach(([label, getter]) => { const row = node("tr"); row.append(node("td", "", () => label)); results.forEach(run => row.append(node("td", "table-number", () => getter(run.report)))); body.append(row); }); table.append(body);
    $("#comparison-table").replaceChildren(table); $("#comparison").hidden = false; $("#comparison").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { toast((error.uiMessage || error.message)); }
  finally { setBusy(button, false); }
}

function openReview(paper, item) {
  state.review = { paper, item, runId: state.run.id };
  const form = $("#review-form"); form.reset();
  $("#review-expression").textContent = item.expression_text;
  $("#review-answer-key").textContent = optionLabel(item, item.answer_id);
  const replacement = contextNode(item.context); replacement.id = "review-context"; $("#review-context").replaceWith(replacement);
  const quality = item.quality?.source === "temporary" ? {} : item.quality || {}, options = $("#review-options"); options.replaceChildren();
  item.options.forEach(option => { const label = node("label", "review-option"), input = node("input"); input.type = "checkbox"; input.name = "accepted_option"; input.value = option.id; input.checked = (quality.accepted_option_ids || []).includes(option.id); label.append(input, node("span", "", () => `${option.id}. ${option.text}`, true)); options.append(label); });
  const fields = $("#rubric-fields"); fields.replaceChildren();
  Object.entries(rubricLabels).forEach(([key, title]) => {
    const row = node("div", "rubric-row"), label = node("label", "", () => title); label.htmlFor = `review-${key}`; label.append(node("small", "", () => rubricDescriptions[key]));
    const select = node("select"); select.id = `review-${key}`; select.name = key; select.required = true;
    const placeholder = node("option", "", () => msg("점수 선택")); placeholder.value = ""; placeholder.disabled = true; select.append(placeholder);
    [0, 1, 2].forEach(score => { const option = node("option", "", () => msg("{0}점 · {1}", score, [msg("미흡"), msg("부분 충족"), msg("충족")][score])); option.value = score; select.append(option); });
    select.value = quality.rubric?.[key] ?? ""; row.append(label, select); fields.append(row);
  });
  Object.keys({ context_sufficient: true, no_specialist_knowledge: true, no_answer_leak: true }).forEach(key => { form.elements[key].checked = quality.gates?.[key] ?? true; });
  let storedReviewer = ""; try { storedReviewer = localStorage.getItem("paperlingo_reviewer") || ""; } catch { /* Storage can be unavailable in private mode. */ }
  $("#reviewer-name").value = quality.source === "human" ? quality.reviewer || storedReviewer : storedReviewer;
  $("#review-rationale").value = quality.source === "human" ? quality.rationale || "" : "";
  showMessage("#review-error", ""); updateReviewPreview(); $("#review-dialog").showModal();
}
async function submitReview(event) {
  event.preventDefault();
  if (!state.review) return;
  const form = $("#review-form"), { paper, item, runId } = state.review;
  const payload = { paper_id: paper.paper_id, question_id: item.question_id, accepted_option_ids: $$("input[name=accepted_option]:checked", form).map(input => input.value), rubric: {}, gates: {}, reviewer: $("#reviewer-name").value.trim(), rationale: $("#review-rationale").value.trim() };
  Object.keys(rubricLabels).forEach(key => { payload.rubric[key] = Number(form.elements[key].value); });
  ["context_sufficient", "no_specialist_knowledge", "no_answer_leak"].forEach(key => { payload.gates[key] = form.elements[key].checked; });
  if (!payload.reviewer || !payload.rationale) { showMessage("#review-error", msg("검수자와 판정 근거를 입력해주세요."), "error"); return; }
  const button = $("#review-submit"); setBusy(button, true, msg("저장 중"));
  try {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}/reviews`, { method: "POST", body: JSON.stringify(payload) });
    try { localStorage.setItem("paperlingo_reviewer", payload.reviewer); } catch { /* Optional local convenience only. */ }
    if (state.run?.id === runId) { state.run = run; renderReport(); }
    $("#review-dialog").close(); state.review = null; toast(msg("검수 결과를 저장하고 점수를 다시 계산했습니다."));
  } catch (error) { showMessage("#review-error", (error.uiMessage || error.message), "error"); }
  finally { setBusy(button, false); }
}

async function loadDataset() {
  const sequence = ++state.datasetSequence;
  const scope = $("#dataset-scope").value;
  const params = new URLSearchParams({ scope, q: $("#dataset-search").value.trim(), offset: state.datasetPage * PAGE_SIZE, limit: PAGE_SIZE });
  const container = $("#dataset-list");
  try {
    const result = await api(`/api/papers?${params}`);
    if (sequence !== state.datasetSequence) return;
    state.datasetTotal = result.total; container.replaceChildren();
    I18n.setText($("#dataset-count"), () => msg("{0} {1}편", scopeLabels[scope], number(result.total)));
    if (!result.items.length) container.append(empty(msg("검색 결과가 없습니다"), msg("다른 논문명, 분야 또는 ID로 검색해보세요.")));
    result.items.forEach(paper => {
      const details = node("details", "dataset-paper"), summary = node("summary"), copy = node("div", "paper-summary-copy");
      copy.append(node("div", "paper-summary-title", () => paper.paper_name || paper.paper_id, true));
      copy.append(node("div", "paper-summary-meta", () => `${paper.paper_id} · ${displayDomain(paper.domain)}`));
      summary.append(copy, Number.isInteger(paper.gold_count) ? tag(msg("표현 {0}개", paper.gold_count), "success") : tag(msg("{0} 자료", scopeLabels[scope]), "neutral")); details.append(summary);
      const body = node("div", "dataset-paper-body"); details.append(body);
      let loaded = false, loading = false;
      details.addEventListener("toggle", async () => {
        if (!details.open || loaded || loading) return;
        loading = true; body.replaceChildren(node("p", "muted", () => msg("논문 데이터를 불러오는 중…")));
        try {
          const full = await api(`/api/papers/${encodeURIComponent(paper.paper_id)}?scope=${encodeURIComponent(scope)}`);
          body.replaceChildren(node("div", "label-caption", () => "ABSTRACT"), node("div", "abstract-text", () => full.abstract, true));
          const expressions = Array.isArray(full.expressions) ? full.expressions : null;
          if (expressions) body.append(node("div", "label-caption", () => "REFERENCE EXPRESSIONS"));
          if (expressions && !expressions.length) body.append(node("p", "muted", () => msg("선정된 참조 표현이 없습니다.")));
          if (!expressions) body.append(node("p", "muted", () => msg("평가용 선정 표현은 비공개입니다. 논문 초록으로 문항을 생성해 평가에 제출하세요.")));
          (expressions || []).forEach(expression => {
            const entry = node("div", "label-entry");
            if (typeof expression === "string") entry.append(node("strong", "", () => expression, true));
            else { entry.append(node("strong", "", () => expression.text, true), tag(expression.expression_type || "expression"), node("p", "", () => expression.reason || "", true)); if (expression.canonical_form) entry.append(node("p", "", () => msg("대표 형태 · {0}", expression.canonical_form))); }
            body.append(entry);
          }); loaded = true;
        } catch (error) { body.replaceChildren(node("p", "notice error", () => I18n.join([(error.uiMessage || error.message), msg(" 닫았다 다시 열어 재시도하세요.")]))); }
        finally { loading = false; }
      }); container.append(details);
    });
    const pages = Math.max(1, Math.ceil(result.total / PAGE_SIZE));
    I18n.setText($("#dataset-page-label"), () => `${state.datasetPage + 1} / ${pages}`);
    $("#dataset-prev").disabled = state.datasetPage === 0;
    $("#dataset-next").disabled = state.datasetPage >= pages - 1;
  } catch (error) { if (sequence === state.datasetSequence) container.replaceChildren(empty(msg("데이터를 불러오지 못했습니다"), (error.uiMessage || error.message))); }
}

function bindEvents() {
  $$("[data-page]").forEach(button => button.addEventListener("click", () => navigate(button.dataset.page)));
  $$("[data-go]").forEach(button => button.addEventListener("click", () => navigate(button.dataset.go)));
  window.addEventListener("hashchange", () => navigate(location.hash.slice(1), false));
  $("#prediction-file").addEventListener("change", event => readFile(event.target.files[0]));
  $("#prediction-text").addEventListener("input", () => { state.isSample = false; $("#demo-notice").hidden = true; });
  const drop = $("#dropzone");
  ["dragenter", "dragover"].forEach(name => drop.addEventListener(name, event => { event.preventDefault(); if (state.session?.can_manage) drop.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach(name => drop.addEventListener(name, event => { event.preventDefault(); drop.classList.remove("dragover"); }));
  drop.addEventListener("drop", event => readFile(event.dataTransfer.files[0]));
  window.addEventListener("dragover", event => event.preventDefault()); window.addEventListener("drop", event => event.preventDefault());
  $("#load-sample").addEventListener("click", loadSample);
  $("#evaluation-form").addEventListener("submit", submitEvaluation);
  $("#judge-button").addEventListener("click", startJudge);
  $("#skip-judge-button").addEventListener("click", skipJudge);
  $("#report-search").addEventListener("input", () => { state.reportPage = 0; renderPapers(); });
  $("#report-filter").addEventListener("change", () => { state.reportPage = 0; renderPapers(); });
  $("#report-prev").addEventListener("click", () => { state.reportPage--; renderPapers(); });
  $("#report-next").addEventListener("click", () => { state.reportPage++; renderPapers(); });
  $("#refresh-history").addEventListener("click", loadHistory);
  $("#compare-button").addEventListener("click", compareRuns);
  $("#review-close").addEventListener("click", () => $("#review-dialog").close());
  $("#review-form").addEventListener("submit", submitReview);
  $("#review-form").addEventListener("change", event => { if (event.target.name === "accepted_option" || event.target.name in rubricLabels || ["context_sufficient", "no_specialist_knowledge", "no_answer_leak"].includes(event.target.name)) updateReviewPreview(); });
  let searchTimer;
  $("#dataset-scope").addEventListener("change", () => { clearTimeout(searchTimer); state.datasetPage = 0; loadDataset(); });
  $("#dataset-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.datasetPage = 0; loadDataset(); }, 280); });
  $("#dataset-prev").addEventListener("click", () => { state.datasetPage--; loadDataset(); });
  $("#dataset-next").addEventListener("click", () => { state.datasetPage++; loadDataset(); });
  $("#auth-button").addEventListener("click", async () => {
    if (state.session?.authenticated && state.session?.can_manage) {
      try { await api("/api/logout", { method: "POST", body: "{}" }); clearTimeout(state.pollTimer); clearTimeout(state.judgeStatusTimer); state.run = null; $("#report-section").hidden = true; await initialize(); toast(msg("로그아웃했습니다.")); }
      catch (error) { toast((error.uiMessage || error.message)); }
    } else openLogin();
  });
  $("#login-close").addEventListener("click", () => $("#login-dialog").close());
  $("#login-dialog").addEventListener("cancel", event => { if (state.session?.auth_required && !state.session?.authenticated && !state.session?.public_read) event.preventDefault(); });
  $("#login-form").addEventListener("submit", async event => {
    event.preventDefault(); const button = $("#login-submit"); setBusy(button, true, msg("확인 중"));
    try { await api("/api/login", { method: "POST", body: JSON.stringify({ password: $("#login-password").value }) }); $("#login-password").value = ""; showMessage("#login-error", ""); $("#login-dialog").close(); await initialize(); if (state.run) renderReport(); toast(msg("팀 워크스페이스에 로그인했습니다.")); }
    catch (error) { showMessage("#login-error", (error.uiMessage || error.message), "error"); }
    finally { setBusy(button, false); }
  });
}

I18n.init();
bindEvents();
navigate(location.hash.slice(1) || "evaluate", false);
initialize();
