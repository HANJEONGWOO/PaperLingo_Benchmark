from http.server import ThreadingHTTPServer
import threading

import httpx
import pytest

from scripts.local_judge_server import RequestError, handler_for, validate_request


def request_body(**updates):
    return {"model": "local-test", "messages": [{"role": "user", "content": "test"}], **updates}


@pytest.mark.parametrize("updates", [
    {"model": "other"}, {"stream": True}, {"n": True}, {"n": 2},
    {"max_tokens": True}, {"max_tokens": 2049}, {"messages": []},
    {"messages": [{"role": ["user"], "content": "x"}]},
    {"messages": [{"role": "user", "content": 123}]},
    {"messages": [{"role": "user", "content": "x" * 40001}]},
    {"response_format": {"type": "json_schema"}}, {"temperature": 0.7},
    {"reasoning_effort": "high"}, {"unknown": "value"},
])
def test_validation_rejects_unsupported_or_unbounded_requests(updates):
    with pytest.raises(RequestError):
        validate_request(request_body(**updates), "local-test", 1536)


def test_validation_allows_app_request_without_hardware_dependencies():
    messages, output_tokens = validate_request(request_body(response_format={"type": "json_object"}), "local-test", 1536)
    assert messages == [{"role": "user", "content": "test"}]
    assert output_tokens == 1536


def test_http_health_models_completion_and_error():
    class FakeRuntime:
        name = "local-test"
        started_at = 0

        def health(self):
            return {"status": "ok", "offline": True}

        def complete(self, payload):
            validate_request(payload, self.name, 1536)
            return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(FakeRuntime()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            assert client.get("/health").json() == {"status": "ok", "offline": True}
            assert client.get("/v1/models").json()["data"][0]["id"] == "local-test"
            assert client.post("/v1/chat/completions", json=request_body()).status_code == 200
            assert client.post("/v1/chat/completions", json=request_body(model="bad")).status_code == 404
            assert client.post("/v1/chat/completions", content="not JSON").status_code == 400
            assert client.get("/unknown").status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
