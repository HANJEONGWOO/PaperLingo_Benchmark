#!/usr/bin/env python3
"""Offline CUDA inference for a cached Qwen3 model; loopback-only OpenAI subset.

No model download is performed. This optional process has independent heavy ML
dependencies; it is not imported by the web application or required by its tests.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import threading
import time
from urllib.parse import urlsplit
import uuid

MAX_REQUEST_BYTES = 96 * 1024


class RequestError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.status = status
        super().__init__(message)


def validate_request(payload: dict, model_name: str, output_limit: int) -> tuple[list[dict], int]:
    if not isinstance(payload, dict):
        raise RequestError("Expected a JSON object.")
    allowed = {"model", "messages", "response_format", "stream", "max_tokens", "max_completion_tokens", "temperature", "seed", "n", "reasoning_effort"}
    if set(payload) - allowed:
        raise RequestError("Unsupported request fields.")
    if payload.get("model") != model_name:
        raise RequestError("Model is not available on this local server.", 404)
    if payload.get("stream", False) is not False or type(payload.get("n", 1)) is not int or payload.get("n", 1) != 1:
        raise RequestError("Only non-streaming single completions are supported.")
    if "response_format" in payload and payload["response_format"] not in ({"type": "json_object"}, {"type": "text"}):
        raise RequestError("response_format supports json_object or text only.")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 16:
        raise RequestError("Provide between 1 and 16 messages.")
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or not isinstance(message.get("role"), str)
            or message["role"] not in {"system", "user", "assistant"}
            or not isinstance(message.get("content"), str)
            or not message["content"]
        ):
            raise RequestError("Messages must contain role and nonempty text content.")
    if sum(len(message["content"]) for message in messages) > 40_000:
        raise RequestError("Message content exceeds the local limit.", 413)
    max_tokens = payload.get("max_completion_tokens", payload.get("max_tokens", output_limit))
    if type(max_tokens) is not int or not 1 <= max_tokens <= output_limit:
        raise RequestError(f"Output token limit must be between 1 and {output_limit}.")
    # Decoding is fixed by the server for reproducible development comparisons.
    if "temperature" in payload and payload["temperature"] != 0:
        raise RequestError("This development server uses fixed temperature 0.")
    if payload.get("reasoning_effort", "none") != "none":
        raise RequestError("This local server uses non-thinking mode.")
    return messages, max_tokens


class Runtime:
    def __init__(self, model_path: str, model_name: str, context_limit: int, output_limit: int):
        # Set before importing Hugging Face so library resolution stays offline.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable. Install a GPU PyTorch build compatible with the host driver.")
        self.torch = torch
        self.name = model_name
        self.context_limit = context_limit
        self.output_limit = output_limit
        self.lock = threading.Lock()
        self.requests = 0
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        logging.info("Loading cached model %s onto CUDA (%s)", model_name, dtype)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_path, local_files_only=True, trust_remote_code=False,
            torch_dtype=dtype, attn_implementation="sdpa",
        ).to("cuda")
        self.model.eval()
        self.started_at = int(time.time())
        self.dtype = str(dtype).replace("torch.", "")
        self.revision = getattr(self.model.config, "_commit_hash", None)
        fingerprint = json.dumps({"revision": self.revision, "torch": torch.__version__, "dtype": self.dtype, "thinking": False, "do_sample": False}, sort_keys=True)
        self.fingerprint = "local-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:24]
        logging.info("Model ready; allocated GPU memory %.2f GiB", torch.cuda.memory_allocated() / 2**30)

    def health(self) -> dict:
        return {
            "status": "ok", "model": self.name, "runtime": "transformers-local",
            "device": "cuda", "dtype": self.dtype, "thinking": False,
            "context_limit": self.context_limit, "output_limit": self.output_limit,
            "requests_completed": self.requests, "busy": self.lock.locked(),
            "gpu_allocated_gib": round(self.torch.cuda.memory_allocated() / 2**30, 2),
            "offline": True,
            "model_revision": self.revision, "torch_version": self.torch.__version__,
            "system_fingerprint": self.fingerprint,
        }

    def complete(self, payload: dict) -> dict:
        messages, max_tokens = validate_request(payload, self.name, self.output_limit)
        if not self.lock.acquire(blocking=False):
            raise RequestError("The local model is busy; retry after the current request.", 503)
        try:
            prompt = self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True, enable_thinking=False,
            )
            inputs = self.tokenizer(prompt, return_tensors="pt", add_special_tokens=False).to("cuda")
            input_tokens = inputs["input_ids"].shape[-1]
            if input_tokens + max_tokens > self.context_limit:
                raise RequestError("Input plus reserved output exceeds the local context limit; shorten the context.")
            with self.torch.inference_mode():
                output = self.model.generate(
                    **inputs, max_new_tokens=max_tokens, do_sample=False,
                    pad_token_id=self.tokenizer.eos_token_id, use_cache=True,
                )
            generated = output[0, input_tokens:]
            completion_tokens = len(generated)
            content = self.tokenizer.decode(generated, skip_special_tokens=True).strip()
            eos = self.model.generation_config.eos_token_id
            eos_ids = set(eos if isinstance(eos, list) else [eos])
            finish_reason = "stop" if generated[-1].item() in eos_ids else "length"
            self.requests += 1
            return {
                "id": "chatcmpl-local-" + uuid.uuid4().hex,
                "object": "chat.completion", "created": int(time.time()), "model": self.name,
                "system_fingerprint": self.fingerprint,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": input_tokens, "completion_tokens": completion_tokens, "total_tokens": input_tokens + completion_tokens},
            }
        except self.torch.cuda.OutOfMemoryError:
            self.torch.cuda.empty_cache()
            raise RequestError("Local GPU memory is insufficient; reduce the context or stop another GPU workload.", 503) from None
        finally:
            self.lock.release()


def handler_for(runtime: Runtime):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PaperLingoLocal/1"

        def setup(self):
            super().setup()
            self.connection.settimeout(330)

        def log_message(self, fmt, *args):
            # Never log headers, bodies, source abstracts, or generated content.
            logging.info("local request %s", args[1] if len(args) > 1 else "")

        def send_json(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                self.send_json(200, runtime.health())
            elif path == "/v1/models":
                self.send_json(200, {"object": "list", "data": [{"id": runtime.name, "object": "model", "created": runtime.started_at, "owned_by": "local"}]})
            else:
                self.send_json(404, {"error": {"message": "Unknown endpoint."}})

        def do_POST(self):
            if urlsplit(self.path).path != "/v1/chat/completions":
                self.send_json(404, {"error": {"message": "Unknown endpoint."}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    raise RequestError("Request body size is invalid.", 413)
                payload = json.loads(self.rfile.read(length))
                result = runtime.complete(payload)
                self.send_json(200, result)
            except RequestError as exc:
                self.send_json(exc.status, {"error": {"message": str(exc), "type": "local_validation"}})
            except (ValueError, UnicodeError):
                self.send_json(400, {"error": {"message": "Malformed JSON request."}})
            except Exception as exc:
                logging.error("Local inference failed: %s", type(exc).__name__)
                self.send_json(500, {"error": {"message": "Local inference failed; inspect runtime availability."}})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", default="Qwen/Qwen3-8B", help="Cached Hugging Face identifier or local model directory")
    parser.add_argument("--model-name", default="Qwen/Qwen3-8B")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1"])
    parser.add_argument("--port", type=int, default=11434)
    parser.add_argument("--context-limit", type=int, default=8192)
    parser.add_argument("--output-limit", type=int, default=3072)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1024 <= args.context_limit <= 8192 or not 128 <= args.output_limit <= 4096 or args.output_limit >= args.context_limit:
        parser.error("Invalid port or token limits.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    runtime = Runtime(args.model_path, args.model_name, args.context_limit, args.output_limit)
    server = ThreadingHTTPServer((args.host, args.port), handler_for(runtime))
    server.daemon_threads = True
    logging.info("Serving cached local judge at http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
