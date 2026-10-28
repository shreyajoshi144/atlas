"""A local fake of the four provider APIs for tests and offline demos.

It speaks the same wire formats the adapters use, over real HTTP on loopback,
so retry/timeout/redirect/size handling is exercised for real. It is NOT a
model: answers come from ``answer_fn`` (default: a canned sentence).
Never used in production code paths.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


def _default_answer(provider: str, req: dict[str, Any]) -> str:
    return "Fake answer from " + provider + "."


class FakeProviderServer:
    def __init__(self, answer_fn: Callable[[str, dict[str, Any]], str] | None = None) -> None:
        self.answer_fn = answer_fn or _default_answer
        self.script: dict[str, deque] = {}          # provider -> queue of actions
        self.requests: list[dict[str, Any]] = []
        self.models: dict[str, list[str]] = {}
        self.usage = {"in": 120, "out": 80}
        self.extra: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.httpd.handle_error = lambda *a, **k: None   # client disconnects are expected in tests
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeProviderServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def push(self, provider: str, *actions: tuple) -> None:
        with self._lock:
            self.script.setdefault(provider, deque()).extend(actions)

    def count(self, provider: str | None = None) -> int:
        return len([r for r in self.requests if r["method"] == "POST" and (provider is None or r["provider"] == provider)])

    # ---- body builders -------------------------------------------------
    def _ok_body(self, provider: str, req: dict[str, Any]) -> dict[str, Any]:
        text = self.answer_fn(provider, req)
        i, o = self.usage["in"], self.usage["out"]
        if provider in ("openai", "xai"):
            body = {"id": "resp_fake", "model": req.get("model"), "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": text, "annotations": []}]}],
                    "usage": {"input_tokens": i, "output_tokens": o, "total_tokens": i + o,
                              "output_tokens_details": {"reasoning_tokens": 10}, "input_tokens_details": {"cached_tokens": 0}}}
            if req.get("tools"):
                body["output"].insert(0, {"type": "web_search_call", "status": "completed"})
                body["output"][1]["content"][0]["annotations"] = [
                    {"type": "url_citation", "url": "https://example.org/a", "title": "Example A", "start_index": 0, "end_index": 5}]
            body.update(self.extra.get(provider, {}))
            return body
        if provider == "anthropic":
            blocks: list[dict[str, Any]] = []
            if req.get("tools"):
                blocks.append({"type": "server_tool_use", "name": "web_search"})
                blocks.append({"type": "web_search_tool_result", "content": [
                    {"type": "web_search_result", "url": "https://example.org/b", "title": "Example B"}]})
                blocks.append({"type": "text", "text": text, "citations": [
                    {"type": "web_search_result_location", "url": "https://example.org/b", "title": "Example B",
                     "cited_text": "cited snippet"}]})
            else:
                blocks.append({"type": "text", "text": text})
            return {"id": "msg_fake", "type": "message", "role": "assistant", "model": req.get("model"),
                    "content": blocks, "stop_reason": "end_turn",
                    "usage": {"input_tokens": i, "output_tokens": o,
                              "server_tool_use": {"web_search_requests": 1 if req.get("tools") else 0}}}
        gm = {}
        if req.get("tools"):
            gm = {"groundingMetadata": {"webSearchQueries": ["q"], "groundingChunks": [
                {"web": {"uri": "https://example.org/c", "title": "example.org"}}]}}
        return {"candidates": [{"content": {"parts": [{"text": "thinking", "thought": True}, {"text": text}]},
                                "finishReason": "STOP", **gm}],
                "usageMetadata": {"promptTokenCount": i, "candidatesTokenCount": o, "thoughtsTokenCount": 7,
                                  "totalTokenCount": i + o + 7},
                "modelVersion": req.get("_model")}

    def _handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # silence
                pass

            def _provider(self) -> str:
                p = self.path
                if p.startswith("/v1beta"):
                    return "gemini"
                if p.startswith("/v1/messages") or (p.startswith("/v1/models") and "x-api-key" in {k.lower() for k in self.headers}):
                    return "anthropic"
                if self.headers.get("X-Provider"):
                    return self.headers["X-Provider"]
                key = self.headers.get("Authorization", "")
                return "xai" if "xai" in key else "openai"

            def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    req = json.loads(raw) if raw else {}
                except ValueError:
                    req = {}
                provider = self._provider()
                if provider == "gemini" and ":generateContent" in self.path:
                    req["_model"] = self.path.split("/models/")[1].split(":")[0]
                    req.setdefault("model", req["_model"])
                with outer._lock:
                    outer.requests.append({"method": method, "path": self.path, "provider": provider,
                                           "headers": {k.lower(): v for k, v in self.headers.items()}, "json": req})
                    q = outer.script.get(provider)
                    action = q.popleft() if q else ("ok",)
                kind = action[0]
                if method == "GET":
                    ids = outer.models.get(provider, [])
                    if provider == "gemini":
                        body = {"models": [{"name": f"models/{m}"} for m in ids]}
                    else:
                        body = {"data": [{"id": m} for m in ids]}
                    return self._send(200, json.dumps(body).encode())
                if kind == "status":
                    _, code, body, *rest = action
                    return self._send(code, json.dumps(body).encode(), rest[0] if rest else None)
                if kind == "raw":
                    return self._send(200, action[1])
                if kind == "redirect":
                    return self._send(302, b"", {"Location": action[1]})
                if kind == "sleep":
                    time.sleep(action[1])
                if kind == "drip":    # trickle bytes slowly (slow-drip server)
                    self.send_response(200)
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                    end = time.monotonic() + action[1]
                    try:
                        while time.monotonic() < end:
                            self.wfile.write(b" ")
                            self.wfile.flush()
                            time.sleep(0.05)
                    except OSError:
                        pass
                    return
                if kind == "big":
                    return self._send(200, b"x" * action[1])
                if kind == "body":     # custom 200 JSON body
                    return self._send(200, json.dumps(action[1]).encode())
                self._send(200, json.dumps(outer._ok_body(provider, req)).encode())

            def do_POST(self):
                self._handle("POST")

            def do_GET(self):
                self._handle("GET")

        return H
