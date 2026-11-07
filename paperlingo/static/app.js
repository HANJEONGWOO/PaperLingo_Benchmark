"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = { session: null, config: null, judgeStatus: null, judgeStatusTimer: null, run: null, runs: [], selectedRuns: new Set(), reportPage: 0, datasetPage: 0, datasetTotal: 0, pollTimer: null, toastTimer: null, datasetSequence: 0, review: null, isSample: false };
const PAGE_SIZE = 10;
const pageLabels = { evaluate: "모델 평가", history: "평가 기록", dataset: "참조 데이터", method: "평가 기준" };
const scopeLabels = { demo: "형식 예제", development: "개발", validation: "검증", test: "최종 평가", all: "전체", custom: "직접 지정" };
const statusLabels = { awaiting_review: "검수 대기", judging: "LLM 검수 중", complete: "완료", needs_review: "재검토 필요", error: "검수 오류", pending: "검수 대기" };
const rubricLabels = { plausibility: "오답의 그럴듯함", learning_alignment: "학습 목표 관련성", distinctness: "오답 간 구별성", no_cues: "불필요한 정답 단서 억제" };
const rubricDescriptions = { plausibility: "B1–B2 학습자가 혼동할 만한 오답인가요?", learning_alignment: "목표 어휘·결합·문법 지식으로 구별하나요?", distinctness: "세 오답이 구별되며 불필요하게 반복되지 않나요?", no_cues: "길이·표기·형식만으로 정답이 드러나지 않나요?" };

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}
function number(value) { return Number(value || 0).toLocaleString("ko-KR"); }
function percent(value, suffix = "%") { return value === null || value === undefined ? "—" : Number(value).toFixed(1) + suffix; }
function date(value) { const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleString("ko-KR", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
function displayDomain(value) { return typeof value === "object" && value ? value.name || value.code || "" : value || ""; }
function tag(text, kind = "neutral") { return node("span", `tag ${kind}`, text); }
function statusTag(status) { return tag(statusLabels[status] || status, status === "complete" ? "success" : status === "error" ? "error" : "warning"); }
function localJudge() { try { return ["localhost", "127.0.0.1", "::1", "[::1]"].includes(new URL(state.config?.judge?.base_url).hostname); } catch { return false; } }
function empty(title, description) { const box = node("div", "empty-state"); box.append(node("strong", "", title), node("p", "", description)); return box; }
function showMessage(selector, message, kind = "") { const element = $(selector); element.textContent = message; element.className = `notice ${kind}`; element.hidden = !message; }
function toast(message) { clearTimeout(state.toastTimer); $("#toast").textContent = message; $("#toast").hidden = false; state.toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 4500); }
function errorText(data, fallback) {
  const detail = data?.detail ?? data;
  if (typeof detail === "string") return detail;
  if (detail && Array.isArray(detail.errors)) return [detail.message, ...detail.errors.map(item => typeof item === "string" ? item : item.message || JSON.stringify(item))].filter(Boolean).join("\n");
  if (Array.isArray(detail)) return detail.map(item => item.msg || item.message || JSON.stringify(item)).join("\n");
  return detail?.message || fallback;
}
async function api(path, options = {}) {
  const headers = { "X-Requested-With": "PaperLingo", ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try { response = await fetch(path, { credentials: "same-origin", ...options, headers }); }
  catch { throw new Error("서버에 연결할 수 없습니다. 연결 상태를 확인하고 다시 시도하세요."); }
  let data;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) {
    if (response.status === 401 && !path.endsWith("/login")) openLogin();
    throw new Error(errorText(data, `요청을 처리하지 못했습니다. (HTTP ${response.status})`));
  }
  return data;
}
function setBusy(button, busy, label) {
  if (busy) { button.dataset.previousLabel = button.textContent; button.disabled = true; button.replaceChildren(node("span", "busy-spin"), document.createTextNode(label)); }
  else { button.disabled = false; button.textContent = button.dataset.previousLabel || label; }
}

function navigate(page, updateHash = true) {
  if (!(page in pageLabels)) page = "evaluate";
  $$(".page").forEach(element => { element.hidden = element.id !== `page-${page}`; });
  $$(".nav-item").forEach(button => { const active = button.dataset.page === page; button.classList.toggle("active", active); if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current"); });
  $("#breadcrumb-current").textContent = pageLabels[page];
  if (updateHash && location.hash !== `#${page}`) history.replaceState(null, "", `#${page}`);
  if (state.config && page === "history") loadHistory();
  if (state.config && page === "dataset") loadDataset();
}
function openLogin() { if (!$("#login-dialog").open) $("#login-dialog").showModal(); }
function updateAccess() {
  const session = state.session || {};
  $$(".manage-only").forEach(button => { button.hidden = !session.can_manage; });
  $("#readonly-banner").hidden = !!session.can_manage;
  $("#access-label").textContent = session.public_access ? "공개 워크스페이스" : session.can_manage ? session.auth_required ? "팀 관리자" : "로컬 워크스페이스" : "읽기 전용";
  $("#auth-button").hidden = !session.auth_required;
  $("#auth-button").textContent = session.authenticated && session.can_manage ? "로그아웃" : "로그인";
  $("#login-close").hidden = session.auth_required && !session.public_read && !session.authenticated;
  $$("#evaluation-form input, #evaluation-form textarea, #evaluation-form select").forEach(input => { input.disabled = !session.can_manage; });
}
async function initialize() {
  try {
    state.session = await api("/api/session");
    updateAccess();
    if (state.session.auth_required && !state.session.authenticated && !state.session.public_read) { openLogin(); $("#connection-status").textContent = "팀 로그인 필요"; return; }
    state.config = await api("/api/config");
    state.judgeStatus = { configured: !!state.config.judge?.configured, ready: false, message: "평가 모델의 연결 상태를 확인 중입니다." };
    const summary = state.config.dataset;
    $("#stat-papers").textContent = number(summary.paper_count);
    $("#stat-expressions").textContent = number(summary.expression_count);
    $("#stat-domains").textContent = number(summary.domain_count);
    $("#dataset-version").textContent = `DATA ${String(summary.version || "v2").slice(0, 12)}`;
    const maxBytes = state.config.limits?.max_upload_bytes;
    if (maxBytes) $("#upload-limit").textContent = `최대 ${Math.round(maxBytes / 1024 / 1024)} MB`;
    $("#connection-status").textContent = "워크스페이스 연결됨";
    $("#connection-dot").style.background = "";
    showMessage("#global-message", "");
    updateScopeHint();
    navigate(location.hash.slice(1) || "evaluate", false);
    refreshJudgeStatus();
  } catch (error) { showMessage("#global-message", error.message, "error"); $("#connection-status").textContent = "연결 확인 필요"; $("#connection-dot").style.background = "#c0856e"; }
}
async function refreshJudgeStatus() {
  clearTimeout(state.judgeStatusTimer);
  if (!state.config) return;
  try {
    state.judgeStatus = await api("/api/judge/status");
  } catch {
    state.judgeStatus = { configured: !!state.config.judge?.configured, ready: false, message: "평가 모델의 연결 상태를 확인할 수 없습니다." };
  }
  const status = state.judgeStatus;
  const label = $("#judge-status-label");
  if (label) {
    label.textContent = status.ready ? `${localJudge() ? "로컬 LLM" : "LLM"} 연결됨 · 검수 가능` : status.configured ? "LLM 준비 대기 · 사람 검수 가능" : "LLM 미설정 · 사람 검수 가능";
    label.className = `tag ${status.ready ? "success" : "neutral"}`;
    label.title = status.message || "";
  }
  if (state.run) renderReport();
  if (status.configured && !status.ready) state.judgeStatusTimer = setTimeout(refreshJudgeStatus, 10000);
}
function updateScopeHint() {
  const scope = $("#scope").value;
  $("#custom-scope-field").hidden = scope !== "custom";
  const rawCount = state.config?.dataset?.splits?.[scope];
  const count = Array.isArray(rawCount) ? rawCount.length : rawCount;
  let message = scope === "custom" ? "평가할 논문 ID를 먼저 고정하세요. 제출 누락도 점수에 반영됩니다." : `${scopeLabels[scope]} 세트${count !== undefined ? ` ${number(count)}편` : ""}의 모든 논문을 평가합니다. 미제출 논문도 Recall의 분모에 포함됩니다.`;
  if (scope === "test") message += " 최종 평가 전에는 생성 프롬프트와 기준을 고정하세요.";
  $("#scope-hint").textContent = message;
}
async function readFile(file) {
  if (!state.session?.can_manage || !file) return;
  const limit = state.config?.limits?.max_upload_bytes || 10 * 1024 * 1024;
  if (file.size > limit) { showMessage("#upload-errors", "파일 크기가 업로드 한도를 초과합니다.", "error"); return; }
  try {
    $("#prediction-text").value = await file.text();
    $("#file-label").textContent = `${file.name} · ${(file.size / 1024).toFixed(1)} KB`;
    state.isSample = false; $("#demo-notice").hidden = true;
    if (!$("#run-name").value) $("#run-name").value = file.name.replace(/\.(jsonl|json)$/i, "").slice(0, 120);
    showMessage("#upload-errors", "");
  } catch { showMessage("#upload-errors", "파일을 읽지 못했습니다. 다시 선택해주세요.", "error"); }
}
async function loadSample() {
  const button = $("#load-sample"); setBusy(button, true, "불러오는 중");
  try {
    const sample = await api("/api/sample");
    $("#prediction-text").value = sample.predictions.map(prediction => JSON.stringify(prediction)).join("\n");
    $("#run-name").value = "입력 형식 예제 · 직접 작성";
    $("#scope").value = sample.scope || "demo"; updateScopeHint();
    $("#paper-ids").value = (sample.paper_ids || []).join("\n");
    $("#file-label").textContent = `직접 작성한 형식 예제 · ${sample.predictions.length}편`;
    state.isSample = true; $("#demo-notice").hidden = false;
    showMessage("#upload-errors", ""); toast("예제를 불러왔습니다. 표현 채점 시작을 누르세요.");
  } catch (error) { showMessage("#upload-errors", error.message, "error"); }
  finally { setBusy(button, false); }
}
async function submitEvaluation(event) {
  event.preventDefault();
  const predictions = $("#prediction-text").value.trim();
  if (!predictions) { showMessage("#upload-errors", "생성 결과 파일을 선택하거나 JSON/JSONL을 붙여넣어주세요.", "error"); $("#prediction-text").focus(); return; }
  const payload = { name: $("#run-name").value.trim(), scope: $("#scope").value, predictions };
  if (payload.scope === "custom") {
    payload.paper_ids = [...new Set($("#paper-ids").value.split(/[\s,]+/).map(value => value.trim()).filter(Boolean))];
    if (!payload.paper_ids.length) { showMessage("#upload-errors", "직접 지정할 평가 대상 논문 ID를 입력해주세요.", "error"); return; }
  }
  const button = $("#evaluate-button"); setBusy(button, true, "표현 채점 중"); showMessage("#upload-errors", "");
  try {
    const run = await api("/api/runs", { method: "POST", body: JSON.stringify(payload) });
    state.run = run; state.reportPage = 0;
    $("#report-search").value = ""; $("#report-filter").value = "all";
    renderReport(); schedulePoll();
    $("#report-section").scrollIntoView({ behavior: "smooth", block: "start" });
    toast("표현 채점이 완료되었습니다. 보기 품질은 별도로 검수하세요.");
  } catch (error) { showMessage("#upload-errors", error.message, "error"); }
  finally { setBusy(button, false); }
}

function metricCard(label, value, detail, featured = false, pending = false, suffix = "%") {
  const card = node("div", `metric-card${featured ? " featured" : ""}`);
  card.append(node("div", "metric-label", label));
  const numeral = node("div", `metric-number${value == null ? " pending" : ""}`);
  if (value == null) numeral.textContent = pending ? "검수 대기" : "해당 없음";
  else numeral.append(document.createTextNode(Number(value).toFixed(1)), node("small", "", suffix));
  card.append(numeral, node("div", "metric-detail", detail));
  const bar = node("div", "metric-bar"); const fill = node("span"); fill.style.width = `${Math.max(0, Math.min(100, Number(value || 0)))}%`; bar.append(fill); card.append(bar); return card;
}
function secondaryMetric(label, value) { const element = node("div", "secondary-metric"); element.append(document.createTextNode(label), node("strong", "", value)); return element; }
function renderReport() {
  const run = state.run;
  if (!run) return;
  const report = run.report, metrics = report.metrics, counts = report.counts;
  const unresolved = (counts.quality_pending || 0) + (counts.quality_error || 0) + (counts.needs_review || 0);
  $("#report-section").hidden = false;
  $("#report-title").textContent = run.name;
  $("#report-meta").textContent = `${date(run.created_at)} · ${scopeLabels[report.scope.name] || report.scope.name} ${number(report.scope.paper_count)}편 · ${statusLabels[run.status] || run.status}`;
  $("#download-json").href = `/api/runs/${encodeURIComponent(run.id)}/export?format=json`;
  $("#download-csv").href = `/api/runs/${encodeURIComponent(run.id)}/export?format=csv`;
  const cards = $("#metric-cards"); cards.replaceChildren(
    metricCard("Precision · 선정 정확도", metrics.precision_pct, `${number(counts.matched)}개 일치 / ${number(counts.predicted)}개 생성`),
    metricCard("Recall · 참조 표현 재현율", metrics.recall_pct, `${number(counts.matched)}개 일치 / ${number(counts.gold)}개 참조`),
    metricCard("F1 · 표현 선정", metrics.f1_pct, "Precision과 Recall의 조화평균"),
    metricCard("Q · 일치 문항 품질", metrics.quality_matched_pct, "참조 라벨과 일치한 문항의 평균", false, unresolved > 0 && counts.matched > 0),
    metricCard("최종 점수", metrics.final_score, "100 × P × R × Q", true, unresolved > 0, "/ 100")
  );
  $("#report-secondary").replaceChildren(
    secondaryMetric("전체 문항 품질", percent(metrics.quality_all_pct)),
    secondaryMetric("단일 정답률", percent(metrics.unique_answer_rate_pct)),
    secondaryMetric("구조 유효율", percent(metrics.structural_validity_pct)),
    secondaryMetric("논문 제출률", percent(metrics.coverage_pct)),
    secondaryMetric("빈 라벨 미생성 정확도", percent(metrics.empty_gold_abstention_pct)),
    secondaryMetric("보기 검수", `${number(counts.quality_complete)} / ${number(counts.predicted)}개 완료`)
  );
  const notices = [];
  if (run.is_demo) notices.push("형식 예제 · 사람이 직접 작성한 입력 형식 예제입니다. 실제 생성 모델의 기준 성능이 아닙니다.");
  if (report.papers.some(paper => paper.items.some(item => item.quality?.source === "llm"))) notices.push("LLM 판정에는 오류가 있을 수 있습니다. 기준 성능 비교 전 문항별 근거와 사람의 표본 검수를 확인하세요.");
  if (run.status === "judging") notices.push(`LLM 보기 검수 진행 중 · ${number(counts.quality_complete)} / ${number(counts.predicted)}개 완료. 결과가 자동으로 갱신됩니다.`);
  else if (unresolved) {
    notices.push(`보기 검수가 ${number(unresolved)}개 남았습니다. 모든 제출 문항의 검수가 완료되면 최종 점수가 확정됩니다.`);
    if (counts.quality_error) notices.push(`API 또는 평가 응답 오류 ${number(counts.quality_error)}개는 재시도하거나 사람이 검수하세요.`);
    if (counts.needs_review) notices.push(`판단이 불확실한 ${number(counts.needs_review)}개는 재검토가 필요합니다.`);
    if (state.config?.judge?.configured && state.judgeStatus?.ready === false) notices.push(`LLM 연결 준비 대기 · ${state.judgeStatus.message || "평가 모델이 아직 연결되지 않았습니다."} 준비 상태를 자동 확인합니다. 사람 검수는 바로 사용할 수 있습니다.`);
    else if (state.config?.judge?.configured) notices.push(`${localJudge() ? "로컬 LLM" : "LLM"} 보기 검수 버튼을 누르면 ${state.config.judge.model} 모델을 사용합니다. 한 번에 최대 ${state.config.limits?.max_questions_per_judge_run || 100}개를 검수합니다.`);
    else if (state.session?.can_manage) notices.push("LLM 평가 모델이 설정되지 않았습니다. 각 문항의 사람 검수 버튼을 이용하거나 서버에 평가 API를 설정하세요.");
  }
  if (counts.missing_papers) notices.push(`평가 대상 중 ${number(counts.missing_papers)}편이 미제출입니다. 해당 참조 표현도 Recall에 포함됩니다.`);
  showMessage("#report-notice", notices.join("\n"), counts.quality_error ? "warning" : "");
  const judgeButton = $("#judge-button");
  judgeButton.hidden = !state.session?.can_manage;
  judgeButton.disabled = !state.config?.judge?.configured || state.judgeStatus?.ready === false || run.status === "judging" || unresolved === 0;
  judgeButton.textContent = run.status === "judging" ? "LLM 검수 진행 중…" : unresolved === 0 ? "보기 검수 완료" : !state.config?.judge?.configured ? "LLM 미설정" : state.judgeStatus?.ready === false ? "LLM 준비 대기" : localJudge() ? "로컬 LLM 보기 검수" : "LLM 보기 검수";
  judgeButton.title = state.judgeStatus?.ready === false ? state.judgeStatus.message || "평가 모델의 연결을 기다리는 중입니다." : !state.config?.judge?.configured ? "서버에서 평가 모델을 설정하세요. 사람 검수는 바로 사용할 수 있습니다." : "설정된 평가 모델로 미완료 문항을 검수합니다.";
  renderPapers();
}
function itemMatchesFilter(item, filter) {
  if (filter === "matched") return item.matched;
  if (filter === "unmatched") return !item.matched;
  if (filter === "pending") return item.quality?.status !== "complete";
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
    if (filter === "missing") return !paper.submitted || paper.missing_expressions?.length;
    if (filter !== "all") return paper.items.some(item => itemMatchesFilter(item, filter));
    return true;
  });
  const pages = Math.max(1, Math.ceil(papers.length / PAGE_SIZE));
  state.reportPage = Math.min(state.reportPage, pages - 1);
  $("#detail-count").textContent = `${number(papers.length)}편`;
  const container = $("#paper-results"); container.replaceChildren();
  if (!papers.length) container.append(empty("조건에 맞는 결과가 없습니다", "검색어 또는 필터를 바꿔보세요."));
  papers.slice(state.reportPage * PAGE_SIZE, (state.reportPage + 1) * PAGE_SIZE).forEach((paper, index) => {
    const details = node("details", "paper-result"); details.dataset.paperId = paper.paper_id;
    details.open = oldOpen.has(paper.paper_id) || (oldOpen.size === 0 && papers.length <= 3 && index === 0);
    const summary = node("summary"), copy = node("div", "paper-summary-copy");
    copy.append(node("div", "paper-summary-title", paper.paper_name || paper.paper_id));
    const meta = node("div", "paper-summary-meta"); meta.append(node("span", "", paper.paper_id), node("span", "", displayDomain(paper.domain)), node("span", "", `생성 ${paper.items.length} / 참조 ${(paper.gold_expressions || []).length}`)); copy.append(meta);
    const tags = node("div", "paper-summary-tags");
    if (!paper.submitted) tags.append(tag("제출 누락", "error"));
    else if (!paper.items.length) tags.append(tag("생성 표현 없음", "neutral"));
    else tags.append(tag(`${paper.items.filter(item => item.matched).length}개 일치`, "success"));
    if (paper.missing_expressions?.length) tags.append(tag(`${paper.missing_expressions.length}개 미일치`, "warning"));
    summary.append(copy, tags); details.append(summary);
    const body = node("div", "paper-body"), goldBlock = node("div", "gold-block");
    goldBlock.append(node("div", "label-caption", "REFERENCE EXPRESSIONS · 참조 표현"));
    const chips = node("div", "chip-list");
    (paper.gold_expressions || []).forEach(expression => { const missing = (paper.missing_expressions || []).includes(expression); const chip = node("span", `expression-chip${missing ? " missing" : ""}`, expression); chip.title = missing ? "생성 결과와 일치하지 않은 참조 표현" : "생성 결과와 일치"; chips.append(chip); });
    if (!paper.gold_expressions?.length) chips.append(node("span", "muted", "참조 표현 없음 · 명시적인 빈 출력을 확인합니다."));
    goldBlock.append(chips); body.append(goldBlock);
    const source = node("details", "abstract-details"); source.append(node("summary", "", "논문 원문 확인"), node("div", "abstract-text", paper.abstract)); body.append(source);
    const visibleItems = paper.items.filter(item => itemMatchesFilter(item, filter));
    visibleItems.forEach(item => body.append(renderQuestion(paper, item)));
    if (!paper.items.length) body.append(node("p", "muted", paper.submitted ? "이 논문은 빈 questions 배열로 제출되었습니다." : "이 논문의 생성 결과가 제출되지 않았습니다."));
    details.append(body); container.append(details);
  });
  $("#report-page-label").textContent = `${state.reportPage + 1} / ${pages}`;
  $("#report-prev").disabled = state.reportPage === 0;
  $("#report-next").disabled = state.reportPage >= pages - 1;
}
function contextNode(context) {
  const block = node("blockquote", "question-context");
  const parts = String(context || "문맥을 구성할 수 없습니다. 학습 표현이 원문에 있고 정답 보기가 표현에 포함되는지 확인하세요.").split("[BLANK]");
  parts.forEach((part, index) => { if (index) block.append(node("span", "blank-marker", "_______")); block.append(document.createTextNode(part)); });
  return block;
}
function renderQuestion(paper, item) {
  const card = node("article", "question-card");
  const heading = node("div", "question-heading"), title = node("div");
  const questionLabel = item.input_format === "compact-v1" ? `문항 ${item.inference.question_position}` : `${item.question_id}${item.expression_type ? ` · ${item.expression_type}` : ""}`;
  title.append(node("span", "question-ident", questionLabel), node("h3", "question-title", item.expression_text));
  const badges = node("div", "question-badges"); badges.append(tag(item.matched ? "라벨 일치" : "라벨 불일치", item.matched ? "success" : "warning"));
  if (!item.structure_valid) badges.append(tag("구조 오류", "error"));
  const quality = item.quality || { status: "pending" };
  badges.append(quality.status === "complete" ? tag(`품질 ${percent(quality.q == null ? null : quality.q * 100)}`, quality.q > 0 ? "success" : "warning") : statusTag(quality.status));
  heading.append(title, badges); card.append(heading, contextNode(item.context));
  const options = node("div", "options-grid");
  (Array.isArray(item.options) ? item.options : []).forEach(option => {
    if (!option || typeof option !== "object" || Array.isArray(option)) { options.append(node("div", "option", "잘못된 보기 형식 · 구조 오류를 확인하세요.")); return; }
    const optionNode = node("div", `option${option.id === item.answer_id ? " correct" : ""}`);
    optionNode.append(node("span", "option-id", option.id), node("span", "option-text", option.text));
    if (option.id === item.answer_id) optionNode.append(node("span", "answer-tag", "생성기 정답")); options.append(optionNode);
  }); card.append(options);
  if (item.issues?.length) { const issues = node("div", "issue-list"); item.issues.forEach(issue => issues.append(node("p", "", `${issue.code} · ${issue.message}`))); card.append(issues); }
  if (quality.source || quality.rationale || quality.option_assessments?.length) {
    const detail = node("details", "quality-detail");
    detail.append(node("summary", "", `검수 근거${quality.source === "human" ? ` · 사람 / ${quality.reviewer || ""}` : quality.source === "llm" ? " · LLM" : quality.source === "structural" ? " · 구조 검증" : ""}`));
    if (quality.rationale) detail.append(node("p", "quality-reason", quality.rationale));
    (quality.option_assessments || []).forEach(assessment => detail.append(node("p", "quality-reason", `${assessment.id} · ${{ acceptable: "허용 가능", incorrect: "오답", uncertain: "불확실" }[assessment.verdict] || assessment.verdict} — ${assessment.reason || ""}`)));
    if (quality.rubric) { const scores = node("div", "rubric-summary"); Object.entries(rubricLabels).forEach(([key, label]) => scores.append(node("span", "", `${label} ${quality.rubric[key]} / 2`))); detail.append(scores); }
    card.append(detail);
  }
  const footer = node("div", "question-footer"); footer.append(node("p", "", item.learning_point || "선정한 전체 표현을 기준으로 라벨 일치 여부를 계산합니다."));
  if (state.session?.can_manage) {
    const review = node("button", "button secondary small", quality.source === "human" ? "검수 수정" : "사람 검수"); review.type = "button"; review.disabled = !item.structure_valid || state.run.status === "judging";
    review.title = !item.structure_valid ? "구조 오류는 입력을 수정하여 새 평가를 제출하세요." : state.run.status === "judging" ? "LLM 검수 완료 후 수정할 수 있습니다." : "동일한 기준으로 문항 품질을 직접 검수합니다.";
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
  } catch (error) { toast(error.message); }
}
function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (state.run?.status !== "judging") return;
  const id = state.run.id;
  state.pollTimer = setTimeout(async () => {
    try {
      const run = await api(`/api/runs/${encodeURIComponent(id)}`);
      if (state.run?.id !== id) return;
      state.run = run; renderReport();
      if (run.status !== "judging") { toast("이번 보기 검수가 끝났습니다. 남은 문항과 재검토 항목을 확인하세요."); if (run.report.counts.quality_error) refreshJudgeStatus(); }
    } catch (error) { showMessage("#report-notice", error.message + " 잠시 후 다시 상태를 확인합니다.", "warning"); }
    schedulePoll();
  }, 2500);
}
async function startJudge() {
  if (!state.run || !state.session?.can_manage) return;
  const button = $("#judge-button"); setBusy(button, true, "검수 시작 중");
  try {
    await api(`/api/runs/${encodeURIComponent(state.run.id)}/judge`, { method: "POST", body: "{}" });
    state.run = await api(`/api/runs/${encodeURIComponent(state.run.id)}`); renderReport(); schedulePoll();
  } catch (error) { setBusy(button, false); showMessage("#report-notice", error.message, "error"); toast(error.message); refreshJudgeStatus(); }
}

async function loadHistory() {
  const container = $("#run-list");
  try {
    const result = await api("/api/runs"); state.runs = result.items || [];
    state.selectedRuns = new Set([...state.selectedRuns].filter(id => state.runs.some(run => run.id === id)));
    container.replaceChildren();
    if (!state.runs.length) { container.append(empty("아직 평가 기록이 없습니다", "모델 평가에서 예제를 불러와 첫 평가를 시작해보세요.")); $("#compare-button").disabled = true; return; }
    const table = node("table"), head = node("thead"), row = node("tr");
    ["선택", "평가 이름", "대상", "상태", "F1", "보기 품질", "최종 점수"].forEach(label => row.append(node("th", "", label))); head.append(row); table.append(head);
    const body = node("tbody");
    state.runs.forEach(run => {
      const tr = node("tr"), checkCell = node("td"), checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = state.selectedRuns.has(run.id); checkbox.setAttribute("aria-label", `${run.name} 비교 선택`);
      checkbox.addEventListener("change", () => { if (checkbox.checked && state.selectedRuns.size >= 2) { checkbox.checked = false; toast("비교할 평가를 최대 2개 선택하세요."); return; } if (checkbox.checked) state.selectedRuns.add(run.id); else state.selectedRuns.delete(run.id); $("#compare-button").disabled = state.selectedRuns.size !== 2; });
      checkCell.append(checkbox); tr.append(checkCell);
      const nameCell = node("td"), button = node("button", "run-name-button", run.name); button.addEventListener("click", () => loadRun(run.id)); nameCell.append(button);
      if (run.is_demo) nameCell.append(document.createTextNode(" "), tag("형식 예제"));
      nameCell.append(node("div", "run-date", date(run.created_at))); tr.append(nameCell);
      const scope = typeof run.scope === "object" ? run.scope?.name : run.scope;
      tr.append(node("td", "", scopeLabels[scope] || scope || "—"));
      const status = node("td"); status.append(statusTag(run.status)); tr.append(status);
      tr.append(node("td", "table-number", percent(run.metrics?.f1_pct)), node("td", "table-number", percent(run.metrics?.quality_matched_pct)), node("td", "table-number", percent(run.metrics?.final_score, ""))); body.append(tr);
    }); table.append(body); container.append(table); $("#compare-button").disabled = state.selectedRuns.size !== 2;
  } catch (error) { container.replaceChildren(empty("평가 기록을 불러오지 못했습니다", error.message)); }
}
async function compareRuns() {
  if (state.selectedRuns.size !== 2) return;
  const button = $("#compare-button"); setBusy(button, true, "비교 준비 중");
  try {
    const results = await Promise.all([...state.selectedRuns].map(id => api(`/api/runs/${encodeURIComponent(id)}`)));
    const [left, right] = results.map(run => run.report);
    const sameScope = JSON.stringify([...left.scope.paper_ids].sort()) === JSON.stringify([...right.scope.paper_ids].sort());
    const comparable = sameScope && left.dataset_version === right.dataset_version && left.metrics_version === right.metrics_version;
    const warnings = [];
    if (!comparable) warnings.push("평가 데이터 버전, 지표 버전 또는 대상 논문이 서로 다릅니다. 아래 수치는 참고용이며 동일 조건의 성능 비교로 해석할 수 없습니다.");
    if (results.some(run => run.is_demo)) warnings.push("직접 작성한 형식 예제가 포함되어 있습니다. 실제 생성 모델의 성능 비교가 아닙니다.");
    if (results.some(run => run.report.metrics.final_score == null)) warnings.push("검수가 미완료되었거나 최종 점수를 정의할 수 없는 평가가 포함되어 있습니다.");
    showMessage("#comparison-warning", warnings.join("\n"), "warning");
    const table = node("table"), head = node("thead"), header = node("tr"); header.append(node("th", "", "비교 항목")); results.forEach(run => header.append(node("th", "", run.name))); head.append(header); table.append(head);
    const rows = [["평가 대상", report => `${scopeLabels[report.scope.name] || report.scope.name} · ${report.scope.paper_count}편`], ["데이터 버전", report => String(report.dataset_version).slice(0, 12)], ["Precision", report => percent(report.metrics.precision_pct)], ["Recall", report => percent(report.metrics.recall_pct)], ["F1", report => percent(report.metrics.f1_pct)], ["일치 문항 품질", report => percent(report.metrics.quality_matched_pct)], ["전체 문항 품질", report => percent(report.metrics.quality_all_pct)], ["단일 정답률", report => percent(report.metrics.unique_answer_rate_pct)], ["논문 제출률", report => percent(report.metrics.coverage_pct)], ["최종 점수", report => percent(report.metrics.final_score, " / 100")]];
    const body = node("tbody"); rows.forEach(([label, getter]) => { const row = node("tr"); row.append(node("td", "", label)); results.forEach(run => row.append(node("td", "table-number", getter(run.report)))); body.append(row); }); table.append(body);
    $("#comparison-table").replaceChildren(table); $("#comparison").hidden = false; $("#comparison").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { toast(error.message); }
  finally { setBusy(button, false); }
}

function openReview(paper, item) {
  state.review = { paper, item, runId: state.run.id };
  const form = $("#review-form"); form.reset();
  $("#review-expression").textContent = `${item.expression_text} · 생성기 정답 ${item.answer_id}`;
  const replacement = contextNode(item.context); replacement.id = "review-context"; $("#review-context").replaceWith(replacement);
  const quality = item.quality || {}, options = $("#review-options"); options.replaceChildren();
  item.options.forEach(option => { const label = node("label", "review-option"), input = node("input"); input.type = "checkbox"; input.name = "accepted_option"; input.value = option.id; input.checked = (quality.accepted_option_ids || []).includes(option.id); label.append(input, node("span", "", `${option.id}. ${option.text}`)); options.append(label); });
  const fields = $("#rubric-fields"); fields.replaceChildren();
  Object.entries(rubricLabels).forEach(([key, title]) => {
    const row = node("div", "rubric-row"), label = node("label", "", title); label.htmlFor = `review-${key}`; label.append(node("small", "", rubricDescriptions[key]));
    const select = node("select"); select.id = `review-${key}`; select.name = key; select.required = true;
    const placeholder = node("option", "", "점수 선택"); placeholder.value = ""; placeholder.disabled = true; select.append(placeholder);
    [0, 1, 2].forEach(score => { const option = node("option", "", `${score}점 · ${["미흡", "부분 충족", "충족"][score]}`); option.value = score; select.append(option); });
    select.value = quality.rubric?.[key] ?? ""; row.append(label, select); fields.append(row);
  });
  Object.keys({ context_sufficient: true, no_specialist_knowledge: true, no_answer_leak: true }).forEach(key => { form.elements[key].checked = quality.gates?.[key] ?? true; });
  let storedReviewer = ""; try { storedReviewer = localStorage.getItem("paperlingo_reviewer") || ""; } catch { /* Storage can be unavailable in private mode. */ }
  $("#reviewer-name").value = quality.source === "human" ? quality.reviewer || storedReviewer : storedReviewer;
  $("#review-rationale").value = quality.source === "human" ? quality.rationale || "" : "";
  showMessage("#review-error", ""); $("#review-dialog").showModal();
}
async function submitReview(event) {
  event.preventDefault();
  if (!state.review) return;
  const form = $("#review-form"), { paper, item, runId } = state.review;
  const payload = { paper_id: paper.paper_id, question_id: item.question_id, accepted_option_ids: $$("input[name=accepted_option]:checked", form).map(input => input.value), rubric: {}, gates: {}, reviewer: $("#reviewer-name").value.trim(), rationale: $("#review-rationale").value.trim() };
  Object.keys(rubricLabels).forEach(key => { payload.rubric[key] = Number(form.elements[key].value); });
  ["context_sufficient", "no_specialist_knowledge", "no_answer_leak"].forEach(key => { payload.gates[key] = form.elements[key].checked; });
  if (!payload.reviewer || !payload.rationale) { showMessage("#review-error", "검수자와 판정 근거를 입력해주세요.", "error"); return; }
  const button = $("#review-submit"); setBusy(button, true, "저장 중");
  try {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}/reviews`, { method: "POST", body: JSON.stringify(payload) });
    try { localStorage.setItem("paperlingo_reviewer", payload.reviewer); } catch { /* Optional local convenience only. */ }
    if (state.run?.id === runId) { state.run = run; renderReport(); }
    $("#review-dialog").close(); state.review = null; toast("검수 결과를 저장하고 점수를 다시 계산했습니다.");
  } catch (error) { showMessage("#review-error", error.message, "error"); }
  finally { setBusy(button, false); }
}

async function loadDataset() {
  const sequence = ++state.datasetSequence;
  const params = new URLSearchParams({ q: $("#dataset-search").value.trim(), offset: state.datasetPage * PAGE_SIZE, limit: PAGE_SIZE });
  const container = $("#dataset-list");
  try {
    const result = await api(`/api/papers?${params}`);
    if (sequence !== state.datasetSequence) return;
    state.datasetTotal = result.total; container.replaceChildren();
    $("#dataset-count").textContent = `${number(result.total)}편`;
    if (!result.items.length) container.append(empty("검색 결과가 없습니다", "다른 논문명, 분야 또는 ID로 검색해보세요."));
    result.items.forEach(paper => {
      const details = node("details", "dataset-paper"), summary = node("summary"), copy = node("div", "paper-summary-copy");
      copy.append(node("div", "paper-summary-title", paper.paper_name || paper.paper_id));
      copy.append(node("div", "paper-summary-meta", `${paper.paper_id} · ${displayDomain(paper.domain)}`));
      summary.append(copy, tag(`표현 ${paper.gold_count ?? 0}개`, "success")); details.append(summary);
      const body = node("div", "dataset-paper-body"); details.append(body);
      let loaded = false, loading = false;
      details.addEventListener("toggle", async () => {
        if (!details.open || loaded || loading) return;
        loading = true; body.replaceChildren(node("p", "muted", "원문과 표현을 불러오는 중…"));
        try {
          const full = await api(`/api/papers/${encodeURIComponent(paper.paper_id)}`);
          body.replaceChildren(node("div", "label-caption", "ABSTRACT"), node("div", "abstract-text", full.abstract));
          body.append(node("div", "label-caption", "REFERENCE EXPRESSIONS"));
          const expressions = full.expressions || [];
          if (!expressions.length) body.append(node("p", "muted", "선정된 참조 표현이 없습니다."));
          expressions.forEach(expression => {
            const entry = node("div", "label-entry");
            if (typeof expression === "string") entry.append(node("strong", "", expression));
            else { entry.append(node("strong", "", expression.text), tag(expression.expression_type || "expression"), node("p", "", expression.reason || "")); if (expression.canonical_form) entry.append(node("p", "", `대표 형태 · ${expression.canonical_form}`)); }
            body.append(entry);
          }); loaded = true;
        } catch (error) { body.replaceChildren(node("p", "notice error", error.message + " 닫았다 다시 열어 재시도하세요.")); }
        finally { loading = false; }
      }); container.append(details);
    });
    const pages = Math.max(1, Math.ceil(result.total / PAGE_SIZE));
    $("#dataset-page-label").textContent = `${state.datasetPage + 1} / ${pages}`;
    $("#dataset-prev").disabled = state.datasetPage === 0;
    $("#dataset-next").disabled = state.datasetPage >= pages - 1;
  } catch (error) { if (sequence === state.datasetSequence) container.replaceChildren(empty("데이터를 불러오지 못했습니다", error.message)); }
}

function bindEvents() {
  $$("[data-page]").forEach(button => button.addEventListener("click", () => navigate(button.dataset.page)));
  $$("[data-go]").forEach(button => button.addEventListener("click", () => navigate(button.dataset.go)));
  window.addEventListener("hashchange", () => navigate(location.hash.slice(1), false));
  $("#scope").addEventListener("change", updateScopeHint);
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
  $("#report-search").addEventListener("input", () => { state.reportPage = 0; renderPapers(); });
  $("#report-filter").addEventListener("change", () => { state.reportPage = 0; renderPapers(); });
  $("#report-prev").addEventListener("click", () => { state.reportPage--; renderPapers(); });
  $("#report-next").addEventListener("click", () => { state.reportPage++; renderPapers(); });
  $("#refresh-history").addEventListener("click", loadHistory);
  $("#compare-button").addEventListener("click", compareRuns);
  $("#review-close").addEventListener("click", () => $("#review-dialog").close());
  $("#review-form").addEventListener("submit", submitReview);
  let searchTimer;
  $("#dataset-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.datasetPage = 0; loadDataset(); }, 280); });
  $("#dataset-prev").addEventListener("click", () => { state.datasetPage--; loadDataset(); });
  $("#dataset-next").addEventListener("click", () => { state.datasetPage++; loadDataset(); });
  $("#auth-button").addEventListener("click", async () => {
    if (state.session?.authenticated && state.session?.can_manage) {
      try { await api("/api/logout", { method: "POST", body: "{}" }); clearTimeout(state.pollTimer); clearTimeout(state.judgeStatusTimer); state.run = null; $("#report-section").hidden = true; await initialize(); toast("로그아웃했습니다."); }
      catch (error) { toast(error.message); }
    } else openLogin();
  });
  $("#login-close").addEventListener("click", () => $("#login-dialog").close());
  $("#login-dialog").addEventListener("cancel", event => { if (state.session?.auth_required && !state.session?.authenticated && !state.session?.public_read) event.preventDefault(); });
  $("#login-form").addEventListener("submit", async event => {
    event.preventDefault(); const button = $("#login-submit"); setBusy(button, true, "확인 중");
    try { await api("/api/login", { method: "POST", body: JSON.stringify({ password: $("#login-password").value }) }); $("#login-password").value = ""; showMessage("#login-error", ""); $("#login-dialog").close(); await initialize(); if (state.run) renderReport(); toast("팀 워크스페이스에 로그인했습니다."); }
    catch (error) { showMessage("#login-error", error.message, "error"); }
    finally { setBusy(button, false); }
  });
}

bindEvents();
navigate(location.hash.slice(1) || "evaluate", false);
initialize();
