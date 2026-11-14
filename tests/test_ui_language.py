"""Optional browser regressions: pip install -r requirements-ui.txt.

Uses an isolated local server/database and never invokes an LLM.
"""
import json
import os
import re
from pathlib import Path
import socket
import threading
import time

import pytest
import uvicorn

pw = pytest.importorskip("playwright.sync_api")
from paperlingo.app import create_app
from paperlingo.config import Settings


@pytest.fixture(scope="module")
def web_url(tmp_path_factory):
    app = create_app(Settings(runtime_dir=tmp_path_factory.mktemp("language-web")))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as playwright:
        candidates = [os.getenv("PAPERLINGO_BROWSER_EXECUTABLE", ""), playwright.chromium.executable_path]
        candidates += sorted(str(p) for p in (Path.home() / ".cache/ms-playwright").glob("chromium-*/chrome-linux*/chrome"))
        executable = next((path for path in candidates if path and Path(path).is_file()), None)
        if executable is None:
            pytest.skip("Install Chromium with python -m playwright install chromium")
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser, web_url):
    context = browser.new_context(locale="en-US", viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(web_url, wait_until="networkidle")
    yield page
    context.close()
    assert not errors


def language(page, code):
    page.locator(f'[data-language="{code}"]').click()
    pw.expect(page.locator("html")).to_have_attribute("lang", code)
    pw.expect(page.locator(f'[data-language="{code}"]')).to_have_attribute("aria-pressed", "true")


def test_updated_page_bypasses_old_unversioned_script(browser, web_url):
    """Simulate an old asset response at the URL used before versioning."""
    static = Path(__file__).resolve().parents[1] / "paperlingo" / "static"
    old_script = (static / "app.js").read_text().replace(
        "const evaluationStats = summary.split_stats.evaluation;",
        "const evaluationStats = summary;",
    )
    with browser.new_context(locale="ko-KR") as context:
        page = context.new_page()
        context.route(web_url + "/static/app.js", lambda route: route.fulfill(
            body=old_script, content_type="application/javascript",
        ))
        page.route(web_url + "/", lambda route: route.fulfill(
            body=(static / "index.html").read_text(), content_type="text/html",
        ))
        page.goto(web_url, wait_until="networkidle")
        pw.expect(page.locator("#stat-papers")).to_have_text("1,490")
        language(page, "en")
        page.unroute(web_url + "/")
        page.reload(wait_until="networkidle")
        pw.expect(page.locator("#stat-papers")).to_have_text("1,044")
        pw.expect(page.locator("#stat-expressions")).to_have_text("5,497")
        pw.expect(page.locator("#stat-domains")).to_have_text("147")
        pw.expect(page.locator("#stat-limit")).to_have_text("10")
        pw.expect(page.locator("html")).to_have_attribute("lang", "en")


def test_saved_choice_and_cross_tab_changes_do_not_reload_or_reset_inputs(page, web_url):
    pw.expect(page.locator("html")).to_have_attribute("lang", "en")
    page.locator("#run-name").fill("모델 평가")  # Must not be mistaken for a UI translation key.
    page.locator('[data-input-mode="paste"]').click()
    page.locator("#prediction-text").fill('[{"paper_id":"keep my input"}]')
    pw.expect(page.locator("#evaluation-form select")).to_have_count(0)
    page.evaluate("window.languageTestMarker = 1234")
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    language(page, "ko")
    language(page, "en")
    assert requests == []
    assert page.evaluate("window.languageTestMarker") == 1234
    pw.expect(page.locator("#run-name")).to_have_value("모델 평가")
    pw.expect(page.locator("#prediction-text")).to_have_value('[{"paper_id":"keep my input"}]')
    language(page, "ko")
    page.reload(wait_until="networkidle")
    pw.expect(page.locator("html")).to_have_attribute("lang", "ko")
    assert page.evaluate("localStorage.getItem('paperlingo_language')") == "ko"
    other = page.context.new_page()
    other.goto(web_url + "/#dataset", wait_until="networkidle")
    pw.expect(other.locator("html")).to_have_attribute("lang", "ko")
    language(other, "en")
    pw.expect(page.locator("html")).to_have_attribute("lang", "en")
    pw.expect(page.locator("#page-method")).to_contain_text("Submission format")
    pw.expect(page.locator('#page-method a[href^="/api/schema"]')).to_have_count(0)


def test_results_checks_filters_and_raw_options_survive_switching(page):
    pw.expect(page.locator("#load-sample")).to_have_count(0)
    predictions = page.request.get(page.url + "api/sample").json()["predictions"]
    question = predictions[0]["questions"][0]
    distractors = [i for i in range(4) if i != question["answer_id"]]
    question["options"][distractors[0]] = question["options"][question["answer_id"]]
    question["options"][distractors[1]] = "통과"
    page.locator('[data-input-mode="paste"]').click()
    page.locator("#prediction-text").fill(json.dumps(predictions, ensure_ascii=False))
    page.locator("#run-name").fill("모델 평가")
    page.locator("#evaluate-button").click()
    pw.expect(page.locator("#report-section")).to_be_visible()
    pw.expect(page.locator(".code-check-list").first).to_contain_text("Duplicates")
    pw.expect(page.locator(".question-badges").first).to_contain_text("Checks failed")
    page.locator("#report-filter").select_option("fail")
    page.locator("#report-search").fill("identical")
    before = page.evaluate("JSON.stringify(state.run)")
    page.locator(".question-card").evaluate("el => el.dataset.preserved = 'yes'")
    language(page, "ko")
    pw.expect(page.locator(".code-check-list").first).to_contain_text("중복 여부")
    language(page, "en")
    pw.expect(page.locator("#report-title")).to_have_text("모델 평가")
    pw.expect(page.locator("#report-filter")).to_have_value("fail")
    pw.expect(page.locator("#report-search")).to_have_value("identical")
    pw.expect(page.locator('.question-card[data-preserved="yes"]')).to_have_count(1)
    pw.expect(page.locator(".option-text", has_text="통과")).to_have_count(1)
    pw.expect(page.locator(".issue-list")).to_contain_text("duplicates after case")
    assert page.evaluate("JSON.stringify(state.run)") == before
    assert page.locator("#paper-results > details").first.get_attribute("open") is not None


def test_report_keeps_secondary_metrics_collapsed_until_requested(page):
    pw.expect(page.locator("#load-sample")).to_have_count(0)
    pw.expect(page.locator("#download-json, #download-csv")).to_have_count(0)
    sample = page.request.get(page.url + "api/sample").json()["predictions"]
    page.locator('[data-input-mode="paste"]').click()
    page.locator("#prediction-text").fill(json.dumps(sample, ensure_ascii=False))
    page.locator("#run-name").fill("metric layout")
    page.locator("#evaluate-button").click()
    pw.expect(page.locator("#report-section")).to_be_visible()
    pw.expect(page.locator("#metric-cards .metric-card")).to_have_count(3)
    pw.expect(page.locator("#metric-cards")).to_contain_text("Final score")
    pw.expect(page.locator("#metric-cards")).to_contain_text("Recall")
    pw.expect(page.locator("#metric-cards")).to_contain_text("Matched-question pass rate")
    secondary = page.locator("#report-secondary details")
    pw.expect(secondary).to_have_count(1)
    assert secondary.evaluate("element => element.open") is False
    secondary.locator("summary").click()
    pw.expect(secondary).to_have_attribute("open", "")
    pw.expect(secondary.locator(".secondary-metric")).to_have_count(4)
    for label in ("Overall question pass rate", "Code checks complete", "Precision", "F1"):
        pw.expect(secondary).to_contain_text(label)
    pw.expect(secondary).not_to_contain_text("Diagnostic")
    language(page, "ko")
    pw.expect(secondary.locator("summary")).to_have_text("보조 지표 숨기기")
    page.evaluate("renderReport()")
    pw.expect(secondary).to_have_attribute("open", "")
    language(page, "en")
    for width in (320, 700, 900, 1440):
        page.set_viewport_size({"width": width, "height": 1000})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_input_tabs_preserve_content_and_support_keyboard_and_upload(page):
    file_tab = page.locator('[data-input-mode="file"]')
    paste_tab = page.locator('[data-input-mode="paste"]')
    pw.expect(page.locator("#input-file-pane")).to_be_visible()
    pw.expect(page.locator("#prediction-text")).to_be_hidden()
    file_tab.focus()
    file_tab.press("ArrowRight")
    pw.expect(paste_tab).to_be_focused()
    pw.expect(paste_tab).to_have_attribute("aria-selected", "true")
    page.locator("#prediction-text").fill('[{"paper_id":"preserve"}]')
    paste_tab.press("Home")
    pw.expect(file_tab).to_be_focused()
    pw.expect(page.locator("#prediction-text")).to_have_value('[{"paper_id":"preserve"}]')
    prediction = json.dumps({"paper_id": "0911.3369", "questions": []})
    page.locator("#prediction-file").set_input_files({
        "name": "submission.jsonl", "mimeType": "application/json", "buffer": prediction.encode(),
    })
    pw.expect(page.locator("#file-label")).to_contain_text("submission.jsonl")
    pw.expect(page.locator("#input-file-pane")).to_be_visible()
    file_tab.press("End")
    pw.expect(page.locator("#prediction-text")).to_have_value(prediction)
    language(page, "ko")
    pw.expect(paste_tab).to_have_attribute("aria-selected", "true")
    pw.expect(page.locator("#prediction-text")).to_have_value(prediction)


def test_validation_messages_translate_in_place(page):
    page.locator("#run-name").fill("validation")
    page.locator("#evaluate-button").click()
    pw.expect(page.locator("#upload-errors")).to_contain_text("Select a predictions file")
    language(page, "ko")
    pw.expect(page.locator("#upload-errors")).to_contain_text("생성 결과 파일을 선택")
    page.locator("#prediction-text").fill("{")
    page.locator("#evaluate-button").click()
    pw.expect(page.locator("#upload-errors")).to_contain_text("JSONL 1행")
    language(page, "en")
    pw.expect(page.locator("#upload-errors")).to_contain_text("JSONL line 1")
    page.locator("#prediction-text").fill('[{"paper_id":"0911.3369","questions":[' + ','.join(['{}'] * 11) + ']}]')
    page.locator("#evaluate-button").click()
    pw.expect(page.locator("#upload-errors")).to_contain_text("At most 10 questions")


def test_data_guide_and_mobile_layout_keep_evaluation_answers_private(page):
    for code, labels in (
        ("en", ["Evaluation papers", "Reference expressions", "Research domains"]),
        ("ko", ["평가 논문", "참조 표현", "연구 분야"]),
    ):
        language(page, code)
        pw.expect(page.locator("#stat-papers")).to_have_text("1,044")
        pw.expect(page.locator("#stat-expressions")).to_have_text("5,497")
        pw.expect(page.locator("#stat-domains")).to_have_text("147")
        pw.expect(page.locator(".dataset-strip > div > span").nth(0)).to_have_text(labels[0])
        pw.expect(page.locator(".dataset-strip > div > span").nth(1)).to_have_text(labels[1])
        pw.expect(page.locator(".dataset-strip > div > span").nth(2)).to_have_text(labels[2])
        for width in (320, 640, 700, 900, 1440):
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    language(page, "en")
    page.locator('[data-page="dataset"]').click()
    pw.expect(page.locator("#dataset-size-evaluation")).to_have_text("1,044")
    pw.expect(page.locator(".evaluation-download-card .download-actions a")).to_have_count(1)
    pw.expect(page.locator(".evaluation-download-card .download-note")).to_contain_text("not provided")
    pw.expect(page.locator("#evaluation-label-download, #evaluation-answer-download")).to_have_count(0)
    # Only the two public examples with reference expressions are shown.
    pw.expect(page.locator("#dataset-list details")).to_have_count(2)
    pw.expect(page.locator("#dataset-list")).not_to_contain_text("Truly Concurrent Process Algebra")
    pw.expect(page.locator("#page-dataset")).to_contain_text("Expression extraction examples")
    page.locator("#dataset-list summary").first.click()
    pw.expect(page.locator("#dataset-list .dataset-paper-body").first).to_contain_text("REFERENCE EXPRESSIONS")
    original = page.locator("#dataset-list .abstract-text").first.inner_text()
    language(page, "ko")
    language(page, "en")
    assert page.locator("#dataset-list .abstract-text").first.inner_text() == original
    assert page.locator("#dataset-list details").first.get_attribute("open") is not None
    for width in (390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator('[data-page="method"]').click()
    pw.expect(page.locator("#page-method")).to_contain_text("How scores are measured")
    pw.expect(page.locator(".method-detail-grid")).to_be_hidden()
    page.locator(".method-details > summary").click()
    pw.expect(page.locator(".method-detail-grid")).to_be_visible()
    pw.expect(page.locator("#page-method")).to_contain_text("How is partial credit given?")
    page.locator(".schema-details > summary").click()
    pw.expect(page.locator(".schema-code")).to_have_count(2)
    pw.expect(page.locator(".schema-code").first).to_be_visible()
    pw.expect(page.locator(".schema-code.jsonl")).to_be_visible()
    pw.expect(page.locator(".schema-code.jsonl")).to_contain_text('"paper_id": "1109.3182"')
    text = page.locator("body").inner_text()
    for jargon in ("soft-rq", "exact-rq", "code-basic", "Qmatched", "PAPERLINGO / V1", "protocol v"):
        assert jargon not in text, jargon
    assert not re.search(r"DATA [0-9a-f]{12}", text)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_switching_during_a_request_restores_translated_button_text(page):
    sample = page.request.get(page.url + "api/sample").json()["predictions"]
    pending = []
    page.route("**/api/runs", lambda route: pending.append(route) if route.request.method == "POST" else route.continue_())
    page.locator("#run-name").fill("busy button")
    page.locator('[data-input-mode="paste"]').click()
    page.locator("#prediction-text").fill(json.dumps(sample, ensure_ascii=False))
    page.locator("#evaluate-button").click()
    pw.expect(page.locator("#evaluate-button")).to_be_disabled()
    language(page, "ko")
    pw.expect(page.locator("#evaluate-button")).to_contain_text("표현 채점 중")
    assert pending
    pending[0].fulfill(response=pending[0].fetch())
    pw.expect(page.locator("#evaluate-button")).to_be_enabled()
    pw.expect(page.locator("#evaluate-button")).to_contain_text("평가 시작")
    pw.expect(page.locator("#report-section")).to_be_visible()
    language(page, "en")
    pw.expect(page.locator("#evaluate-button")).to_contain_text("Start evaluation")


def test_example_downloads_and_submission_score_full_marks_in_both_languages(page, tmp_path):
    page.locator('[data-page="dataset"]').click()
    pw.expect(page.locator("#dataset-size-demo")).to_have_text("2")
    pw.expect(page.locator("#dataset-size-evaluation")).to_have_text("1,044")
    pw.expect(page.locator("#dataset-scope")).to_have_count(0)
    pw.expect(page.locator("#dataset-search")).to_have_count(0)
    pw.expect(page.locator("#evaluation-form select")).to_have_count(0)
    downloads = {}
    for kind in ("papers", "labels", "answers"):
        with page.expect_download() as event:
            page.locator(f'a[href="/api/datasets/demo/{kind}"]').click()
        download = event.value
        path = tmp_path / download.suggested_filename
        download.save_as(path)
        downloads[kind] = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(downloads[kind]) == 2
        assert "1709.01217" not in json.dumps(downloads[kind])
    assert [row["paper_id"] for row in downloads["papers"]] == [row["paper_id"] for row in downloads["labels"]] == [row["paper_id"] for row in downloads["answers"]]
    assert sum(len(row["questions"]) for row in downloads["answers"]) == 9
    language(page, "ko")
    pw.expect(page.locator("#example-download-description")).to_contain_text("논문 2편")
    page.locator('[data-page="evaluate"]').click()
    page.locator("#run-name").fill("예시 다운로드 검증")
    page.locator('[data-input-mode="paste"]').click()
    page.locator("#prediction-text").fill(json.dumps(downloads["answers"], ensure_ascii=False))
    page.locator("#evaluate-button").click()
    pw.expect(page.locator("#report-section")).to_be_visible()
    page.wait_for_function("state.run?.report.metrics.final_score === 100")
    assert page.evaluate("state.run.report.counts.code_passed") == 9
    assert page.evaluate("state.run.report.metrics.recall_pct") == 100
    language(page, "en")
    pw.expect(page.locator("#metric-cards")).to_contain_text("Matched-question pass rate")
    assert page.evaluate("state.run.report.metrics.final_score") == 100


def test_storage_denied_still_allows_switching(browser, web_url):
    context = browser.new_context(locale="ko-KR")
    context.add_init_script("Object.defineProperty(window, 'localStorage', {get() {throw new DOMException('blocked', 'SecurityError');}})")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(web_url, wait_until="networkidle")
    pw.expect(page.locator("html")).to_have_attribute("lang", "ko")
    language(page, "en")
    pw.expect(page.locator("#evaluate-button")).to_contain_text("Start evaluation")
    context.close()
    assert not errors


def test_upload_replaces_pasted_text_and_uses_file_ids_without_a_scope_selector(page):
    pw.expect(page.locator("#evaluation-form select")).to_have_count(0)
    sample_text = json.dumps(page.request.get(page.url + "api/sample").json()["predictions"])
    page.locator('[data-input-mode="paste"]').click()
    page.locator("#prediction-text").fill(sample_text)
    pw.expect(page.locator("#prediction-text")).not_to_have_value("")
    evaluation_id = page.request.get(page.url + "api/papers?scope=evaluation&limit=1").json()["items"][0]["paper_id"]
    prediction = json.dumps({"paper_id": evaluation_id, "questions": []})
    page.locator("#prediction-file").set_input_files({
        "name": "evaluation.jsonl", "mimeType": "application/json", "buffer": prediction.encode(),
    })
    pw.expect(page.locator("#prediction-text")).to_have_value(prediction)
    with page.expect_response(lambda response: response.url.endswith("/api/runs") and response.request.method == "POST") as created:
        page.locator("#evaluate-button").click()
    response = created.value
    assert response.status == 201
    assert "scope" not in response.request.post_data_json
    report = response.json()["report"]
    assert report["scope"]["name"] == "evaluation"
    assert report["scope"]["paper_count"] == 1044
    assert report["counts"]["gold"] == 5497
    pw.expect(page.locator("#report-meta")).to_contain_text("Evaluation · 1,044 papers")
    # Pasting downloaded examples after a real evaluation must also re-detect.
    page.locator("#prediction-text").fill(sample_text)
    with page.expect_response(lambda response: response.url.endswith("/api/runs") and response.request.method == "POST") as created:
        page.locator("#evaluate-button").click()
    assert created.value.json()["report"]["scope"]["name"] == "demo"
    assert created.value.json()["report"]["metrics"]["final_score"] == 100
