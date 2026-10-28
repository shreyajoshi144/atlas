"""Provider adapters: build each provider's real HTTP request and normalize its response.

Endpoints / shapes (verified against each provider's docs on 2026-10-20; NOT live-tested here):
* OpenAI     POST /v1/responses            (Responses API; Authorization: Bearer)
* xAI        POST /v1/responses            (OpenAI-compatible Responses API; Bearer)
* Anthropic  POST /v1/messages             (x-api-key + anthropic-version)
* Gemini     POST /v1beta/models/{m}:generateContent   (x-goog-api-key)

Sampling parameters (temperature etc.) are deliberately not sent: several current models reject
them, and every provider's default keeps the comparison apples-to-apples. Server-side storage is
disabled where the API offers it (``store: false``).
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from .catalog import ModelEntry
from .errors import ProviderError, classify_http
from .settings import ProviderSettings
from .models import Citation, NormalizedResponse, ProviderRequest, Usage

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_SEARCH_TOOL = "web_search_20250305"
ANTHROPIC_MAX_SEARCHES = 3


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _trim(s: Any, n: int = 300) -> str | None:
    return s[:n] if isinstance(s, str) and s else None


def _http_url(u: Any) -> str | None:
    return u if isinstance(u, str) and u.lower().startswith(("http://", "https://")) else None


def _dedupe(cites: list[Citation]) -> list[Citation]:
    seen, out = set(), []
    for c in cites:
        key = c.url or c.title
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return out


class Adapter:
    provider = ""

    def build(self, entry: ModelEntry, req: ProviderRequest, s: ProviderSettings) -> tuple[str, dict[str, str], dict[str, Any]]:
        raise NotImplementedError

    def parse(self, entry: ModelEntry, data: dict[str, Any]) -> NormalizedResponse:
        raise NotImplementedError

    def error_from(self, status: int, headers: dict[str, str], body: bytes) -> ProviderError:
        code, text = None, ""
        try:
            data = json.loads(body.decode("utf-8", "replace"))
            err = data.get("error") if isinstance(data, dict) else None
            if isinstance(err, dict):
                code = err.get("code") or err.get("type") or err.get("status")
                text = str(err.get("message", ""))[:400].lower()
            elif isinstance(err, str):
                text = err[:400].lower()
        except Exception:
            text = body[:200].decode("utf-8", "replace").lower()
        retry_after = None
        ra = headers.get("retry-after")
        if ra:
            try:
                retry_after = max(0.0, float(ra))
            except ValueError:
                retry_after = None
        category = classify_http(status, code if isinstance(code, str) else None, text)
        # Provider-specific: 404 whose message is about the model => model_not_found (already), else keep.
        return ProviderError(category, http_status=status, retry_after=retry_after, provider=self.provider,
                             detail_code=code if isinstance(code, str) else None)

    # model discovery ------------------------------------------------------
    def list_models_request(self, s: ProviderSettings, page_token: str | None = None) -> tuple[str, dict[str, str]]:
        raise NotImplementedError

    def parse_models(self, data: dict[str, Any]) -> tuple[set[str], str | None]:
        raise NotImplementedError


class OpenAICompatAdapter(Adapter):
    """OpenAI and xAI share the Responses API shape."""

    def __init__(self, provider: str) -> None:
        self.provider = provider

    def build(self, entry, req, s):
        body: dict[str, Any] = {
            "model": entry.model,
            "instructions": req.system,
            "input": req.user,
            "max_output_tokens": req.max_output_tokens,
            "store": False,
        }
        if req.native_search:
            body["tools"] = [{"type": "web_search"}]
        if req.json_schema and not req.native_search:
            body["text"] = {"format": {"type": "json_schema", "name": "atlas_answer",
                                       "schema": req.json_schema, "strict": True}}
        headers = {"Authorization": f"Bearer {s.key(self.provider)}", "Content-Type": "application/json"}
        return f"{s.base_url(self.provider)}/v1/responses", headers, body

    def parse(self, entry, data):
        if not isinstance(data, dict):
            raise ProviderError("parse", provider=self.provider)
        err = data.get("error")
        if err:
            code = err.get("code") if isinstance(err, dict) else None
            raise ProviderError(classify_http(500 if not code else 400, code if isinstance(code, str) else None),
                                provider=self.provider, detail_code=code if isinstance(code, str) else None)
        if "output" not in data and "status" not in data and "usage" not in data:
            raise ProviderError("parse", provider=self.provider)
        texts: list[str] = []
        cites: list[Citation] = []
        refusal = False
        searches = 0
        for item in data.get("output") or []:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t in ("web_search_call", "x_search_call"):
                searches += 1
            if t != "message":
                continue
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "refusal":
                    refusal = True
                    continue
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                    for a in part.get("annotations") or []:
                        if isinstance(a, dict) and a.get("type") == "url_citation" and _http_url(a.get("url")):
                            cites.append(Citation("provider_web", a["url"], _trim(a.get("title"), 200)))
        for u in data.get("citations") or []:       # some xAI responses expose a flat list
            url = u if isinstance(u, str) else (u.get("url") if isinstance(u, dict) else None)
            if _http_url(url):
                cites.append(Citation("provider_web", url, _trim(u.get("title"), 200) if isinstance(u, dict) else None))
        u = data.get("usage") or {}
        out_details = u.get("output_tokens_details") or {}
        in_details = u.get("input_tokens_details") or {}
        provider_cost = None
        ticks = u.get("cost_in_usd_ticks")
        if isinstance(ticks, (int, float)) and ticks >= 0:
            provider_cost = ticks / 1e10
        usage = Usage(
            input_tokens=_int(u.get("input_tokens")), output_tokens=_int(u.get("output_tokens")),
            reasoning_tokens=_int(out_details.get("reasoning_tokens")),
            cached_input_tokens=_int(in_details.get("cached_tokens")), total_tokens=_int(u.get("total_tokens")),
            search_calls=searches, provider_cost_usd=provider_cost)
        status = data.get("status")
        reason = (data.get("incomplete_details") or {}).get("reason")
        truncated = status == "incomplete" and reason in ("max_output_tokens", "max_tokens", None)
        if status == "failed":
            raise ProviderError("server", provider=self.provider)
        finish = "refusal" if refusal and not texts else ("content_filter" if reason == "content_filter"
                                                           else ("length" if truncated else "stop"))
        return NormalizedResponse("\n".join(texts).strip(), finish, usage, _dedupe(cites), truncated,
                                  self.provider, entry.model, served_model=data.get("model") if isinstance(data.get("model"), str) else None)

    def list_models_request(self, s, page_token=None):
        return f"{s.base_url(self.provider)}/v1/models", {"Authorization": f"Bearer {s.key(self.provider)}"}

    def parse_models(self, data):
        ids: set[str] = set()
        for m in (data.get("data") or []) if isinstance(data, dict) else []:
            if isinstance(m, dict) and isinstance(m.get("id"), str):
                ids.add(m["id"])
                for a in m.get("aliases") or []:
                    if isinstance(a, str):
                        ids.add(a)
        return ids, None


class AnthropicAdapter(Adapter):
    provider = "anthropic"

    def build(self, entry, req, s):
        body: dict[str, Any] = {
            "model": entry.model,
            "max_tokens": req.max_output_tokens,
            "system": req.system,
            "messages": [{"role": "user", "content": req.user}],
        }
        if req.native_search:
            body["tools"] = [{"type": ANTHROPIC_SEARCH_TOOL, "name": "web_search", "max_uses": ANTHROPIC_MAX_SEARCHES}]
        headers = {"x-api-key": s.key("anthropic") or "", "anthropic-version": ANTHROPIC_VERSION,
                   "Content-Type": "application/json"}
        return f"{s.base_url('anthropic')}/v1/messages", headers, body

    def parse(self, entry, data):
        if not isinstance(data, dict) or data.get("type") == "error" or not isinstance(data.get("content"), list):
            raise ProviderError("parse", provider=self.provider)
        texts: list[str] = []
        cites: list[Citation] = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
                for c in block.get("citations") or []:
                    if isinstance(c, dict) and _http_url(c.get("url")):
                        cites.append(Citation("provider_web", c["url"], _trim(c.get("title"), 200), _trim(c.get("cited_text"), 200)))
            elif bt == "web_search_tool_result" and isinstance(block.get("content"), list):
                for r in block["content"]:
                    if isinstance(r, dict) and r.get("type") == "web_search_result" and _http_url(r.get("url")):
                        cites.append(Citation("provider_web", r["url"], _trim(r.get("title"), 200)))
        u = data.get("usage") or {}
        cache_read, cache_write = _int(u.get("cache_read_input_tokens")) or 0, _int(u.get("cache_creation_input_tokens")) or 0
        base_in = _int(u.get("input_tokens"))
        searches = _int((u.get("server_tool_use") or {}).get("web_search_requests")) or 0
        usage = Usage(
            input_tokens=None if base_in is None else base_in + cache_read + cache_write,
            output_tokens=_int(u.get("output_tokens")), cached_input_tokens=cache_read or None, search_calls=searches)
        if usage.input_tokens is not None and usage.output_tokens is not None:
            usage.total_tokens = usage.input_tokens + usage.output_tokens
        stop = data.get("stop_reason")
        finish = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "refusal": "refusal",
                  "pause_turn": "tool_limit", "tool_use": "tool_limit"}.get(stop, "other")
        truncated = stop in ("max_tokens", "pause_turn")
        return NormalizedResponse("".join(texts).strip(), finish, usage, _dedupe(cites), truncated,
                                  self.provider, entry.model, served_model=data.get("model") if isinstance(data.get("model"), str) else None)

    def list_models_request(self, s, page_token=None):
        url = f"{s.base_url('anthropic')}/v1/models?limit=1000" + (f"&after_id={quote(page_token)}" if page_token else "")
        return url, {"x-api-key": s.key("anthropic") or "", "anthropic-version": ANTHROPIC_VERSION}

    def parse_models(self, data):
        ids = {m["id"] for m in (data.get("data") or []) if isinstance(m, dict) and isinstance(m.get("id"), str)}
        nxt = data.get("last_id") if data.get("has_more") else None
        return ids, nxt if isinstance(nxt, str) else None


class GeminiAdapter(Adapter):
    provider = "gemini"

    def build(self, entry, req, s):
        gen: dict[str, Any] = {"maxOutputTokens": req.max_output_tokens}
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": req.system}]},
            "contents": [{"role": "user", "parts": [{"text": req.user}]}],
            "generationConfig": gen,
        }
        if req.native_search:
            body["tools"] = [{"google_search": {}}]
        elif req.json_schema:
            gen["responseMimeType"] = "application/json"     # schema itself is validated locally
        headers = {"x-goog-api-key": s.key("gemini") or "", "Content-Type": "application/json"}
        return f"{s.base_url('gemini')}/v1beta/models/{quote(entry.model)}:generateContent", headers, body

    def parse(self, entry, data):
        if not isinstance(data, dict):
            raise ProviderError("parse", provider=self.provider)
        block = (data.get("promptFeedback") or {}).get("blockReason")
        cands = data.get("candidates") or []
        if not cands:
            if block:
                raise ProviderError("content_blocked", provider=self.provider, detail_code=str(block)[:40])
            raise ProviderError("empty", provider=self.provider)
        cand = cands[0] if isinstance(cands[0], dict) else {}
        texts = [p["text"] for p in ((cand.get("content") or {}).get("parts") or [])
                 if isinstance(p, dict) and isinstance(p.get("text"), str) and not p.get("thought")]
        cites: list[Citation] = []
        gm = cand.get("groundingMetadata") or {}
        chunks = gm.get("groundingChunks") or []
        for ch in chunks:
            web = ch.get("web") if isinstance(ch, dict) else None
            if isinstance(web, dict) and _http_url(web.get("uri")):
                cites.append(Citation("provider_web", web["uri"], _trim(web.get("title"), 200)))
        um = data.get("usageMetadata") or {}
        cand_tokens, thoughts = _int(um.get("candidatesTokenCount")), _int(um.get("thoughtsTokenCount"))
        out_tokens = None if cand_tokens is None and thoughts is None else (cand_tokens or 0) + (thoughts or 0)
        queries = gm.get("webSearchQueries") or []
        usage = Usage(input_tokens=_int(um.get("promptTokenCount")), output_tokens=out_tokens,
                      reasoning_tokens=thoughts, cached_input_tokens=_int(um.get("cachedContentTokenCount")),
                      total_tokens=_int(um.get("totalTokenCount")), search_calls=len(queries))
        fr = cand.get("finishReason")
        finish = {"STOP": "stop", "MAX_TOKENS": "length"}.get(fr, "content_filter" if fr in (
            "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY") else "other")
        return NormalizedResponse("".join(texts).strip(), finish, usage, _dedupe(cites), fr == "MAX_TOKENS",
                                  self.provider, entry.model,
                                  served_model=data.get("modelVersion") if isinstance(data.get("modelVersion"), str) else None)

    def list_models_request(self, s, page_token=None):
        url = f"{s.base_url('gemini')}/v1beta/models?pageSize=1000" + (f"&pageToken={quote(page_token)}" if page_token else "")
        return url, {"x-goog-api-key": s.key("gemini") or ""}

    def parse_models(self, data):
        ids = set()
        for m in data.get("models") or []:
            if isinstance(m, dict) and isinstance(m.get("name"), str):
                ids.add(m["name"].removeprefix("models/"))
        nxt = data.get("nextPageToken")
        return ids, nxt if isinstance(nxt, str) and nxt else None


ADAPTERS: dict[str, Adapter] = {
    "openai": OpenAICompatAdapter("openai"),
    "xai": OpenAICompatAdapter("xai"),
    "anthropic": AnthropicAdapter(),
    "gemini": GeminiAdapter(),
}
