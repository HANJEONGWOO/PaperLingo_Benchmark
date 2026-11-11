"""Optional browser regressions: pip install -r requirements-ui.txt.

Uses an isolated local server/database and never invokes an LLM.
"""
import json
import os
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
        pw.expect(page.locator("#stat-expressions")).to_have_text("4,832")
        pw.expect(page.locator("#stat-domains")).to_have_text("147")
        pw.expect(page.locator("html")).to_have_attribute("lang", "en")


def test_saved_choice_and_cross_tab_changes_do_not_reload_or_reset_inputs(page, web_url):
    pw.expect(page.locator("html")).to_have_attribute("lang", "en")
    page.locator("#run-name").fill("모델 평가")  # Must not be mistaken for a UI translation key.
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
    pw.expect(page.locator('#page-method a[href="/api/schema?lang=en"]')).to_have_count(1)


def test_results_checks_filters_and_raw_options_survive_switching(page):
    page.locator("#load-sample").click()
    pw.expect(page.locator("#prediction-text")).not_to_have_value("")
    predictions = [json.loads(line) for line in page.locator("#prediction-text").input_value().splitlines()]
    question = predictions[0]["questions"][0]
    distractors = [i for i in range(4) if i != question["answer_id"]]
    question["options"][distractors[0]] = question["options"][question["answer_id"]]
    question["options"][distractors[1]] = "통과"
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
    page.locator("#prediction-text").fill('[{"paper_id":"0911.3369","questions":[' + ','.join(['{}'] * 6) + ']}]')
    page.locator("#evaluate-button").click()
    pw.expect(page.locator("#upload-errors")).to_contain_text("At most 5 questions")


def test_data_guide_and_mobile_layout_keep_evaluation_downloads_disabled(page):
    for code, labels in (
        ("en", ["Evaluation papers", "Evaluation expressions", "Evaluation domains"]),
        ("ko", ["평가 논문", "평가 참조 표현", "평가 연구 분야"]),
    ):
        language(page, code)
        pw.expect(page.locator("#stat-papers")).to_have_text("1,044")
        pw.expect(page.locator("#stat-expressions")).to_have_text("4,832")
        pw.expect(page.locator("#stat-domains")).to_have_text("147")
        pw.expect(page.locator(".dataset-strip > div > span").nth(0)).to_have_text(labels[0])
        pw.expect(page.locator(".dataset-strip > div > span").nth(1)).to_have_text(labels[1])
        pw.expect(page.locator(".dataset-strip > div > span").nth(2)).to_have_text(labels[2])
        for width in (320, 1440):
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    language(page, "en")
    page.locator('[data-page="dataset"]').click()
    pw.expect(page.locator("#dataset-size-evaluation")).to_have_text("1,044")
    pw.expect(page.locator("#evaluation-label-download")).to_have_text("Selected expressions ↓")
    pw.expect(page.locator("#evaluation-answer-download")).to_be_disabled()
    page.locator("#dataset-scope").select_option("evaluation")
    pw.expect(page.locator("#dataset-count")).to_have_text("Evaluation · 1,044 papers")
    page.locator("#dataset-list summary").first.click()
    pw.expect(page.locator("#dataset-list .dataset-paper-body").first).to_contain_text("Evaluation reference expressions are private.")
    original = page.locator("#dataset-list .abstract-text").first.inner_text()
    language(page, "ko")
    language(page, "en")
    assert page.locator("#dataset-list .abstract-text").first.inner_text() == original
    assert page.locator("#dataset-list details").first.get_attribute("open") is not None
    pw.expect(page.locator("#evaluation-label-download")).to_be_disabled()
    for width in (390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator('[data-page="method"]').click()
    pw.expect(page.locator("#page-method")).to_contain_text("Every score should be explainable.")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_switching_during_a_request_restores_translated_button_text(page):
    sample = page.request.get(page.url + "api/sample").json()
    pending = []
    page.route("**/api/sample", lambda route: pending.append(route))
    page.locator("#load-sample").click()
    pw.expect(page.locator("#load-sample")).to_be_disabled()
    language(page, "ko")
    pw.expect(page.locator("#load-sample")).to_contain_text("불러오는 중")
    assert pending
    pending[0].fulfill(json=sample)
    pw.expect(page.locator("#load-sample")).to_be_enabled()
    pw.expect(page.locator("#load-sample")).to_contain_text("예제 불러오기")
    language(page, "en")
    pw.expect(page.locator("#load-sample")).to_contain_text("Load example")


def test_example_downloads_and_submission_score_full_marks_in_both_languages(page, tmp_path):
    page.locator('[data-page="dataset"]').click()
    pw.expect(page.locator("#dataset-size-demo")).to_have_text("3")
    pw.expect(page.locator("#dataset-size-evaluation")).to_have_text("1,044")
    assert page.locator("#dataset-scope option").evaluate_all("els => els.map(el => el.value)") == ["demo", "evaluation"]
    pw.expect(page.locator("#evaluation-form select")).to_have_count(0)
    downloads = {}
    for kind in ("papers", "labels", "answers"):
        with page.expect_download() as event:
            page.locator(f'a[href="/api/datasets/demo/{kind}"]').click()
        download = event.value
        path = tmp_path / download.suggested_filename
        download.save_as(path)
        downloads[kind] = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(downloads[kind]) == 3
    assert [row["paper_id"] for row in downloads["papers"]] == [row["paper_id"] for row in downloads["labels"]] == [row["paper_id"] for row in downloads["answers"]]
    assert sum(len(row["questions"]) for row in downloads["answers"]) == 9
    language(page, "ko")
    pw.expect(page.locator("#example-download-description")).to_contain_text("100점")
    page.locator('[data-page="evaluate"]').click()
    page.locator("#run-name").fill("예시 다운로드 검증")
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
    pw.expect(page.locator("#load-sample")).to_contain_text("Load example")
    context.close()
    assert not errors


def test_upload_after_example_load_uses_file_ids_without_a_scope_selector(page):
    pw.expect(page.locator("#evaluation-form select")).to_have_count(0)
    page.locator("#load-sample").click()
    pw.expect(page.locator("#prediction-text")).not_to_have_value("")
    sample_text = page.locator("#prediction-text").input_value()
    manifest = page.request.get(page.url + "api/splits").json()
    evaluation_id = manifest["splits"]["evaluation"][0]
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
    assert report["counts"]["gold"] == 4832
    pw.expect(page.locator("#report-meta")).to_contain_text("Evaluation · 1,044 papers")
    # Pasting downloaded examples after a real evaluation must also re-detect.
    page.locator("#prediction-text").fill(sample_text)
    with page.expect_response(lambda response: response.url.endswith("/api/runs") and response.request.method == "POST") as created:
        page.locator("#evaluate-button").click()
    assert created.value.json()["report"]["scope"]["name"] == "demo"
    assert created.value.json()["report"]["metrics"]["final_score"] == 100
