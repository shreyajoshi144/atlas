"""ProviderClient against a real local HTTP fake: retries, timeouts, redirects, breaker, parsing."""
import asyncio
import time
import unittest

from backend.providers.client import ProviderClient
from backend.providers.errors import ProviderError
from backend.providers.models import ProviderRequest
from backend.providers.testing.fake_provider_server import FakeProviderServer
from backend.providers.tests.helpers import CATALOG, KEYS, make_settings

REQ = ProviderRequest(system="sys", user="hello", max_output_tokens=256, request_id="req1")


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.srv = FakeProviderServer().start()
        self.addCleanup(self.srv.stop)

    def client(self, **over):
        return ProviderClient(make_settings(self.srv, **over))

    async def test_all_four_providers_normalize(self):
        c = self.client()
        for mid in ("openai:gpt-6.1-sol", "anthropic:claude-sonnet-5-5", "gemini:gemini-4-argon", "xai:grok-4.7"):
            r = await c.generate(CATALOG.get(mid), REQ)
            self.assertIn("Fake answer", r.text, mid)
            self.assertEqual(r.finish_reason, "stop")
            self.assertEqual(r.usage.input_tokens, 120)
            self.assertTrue(r.usage.reported)
            self.assertEqual(len(r.attempts), 1)
            self.assertGreaterEqual(r.total_ms, 0)
        # gemini output = candidates + thoughts; thought part excluded from text
        g = await c.generate(CATALOG.get("gemini:gemini-4-argon"), REQ)
        self.assertEqual(g.usage.output_tokens, 87)
        self.assertNotIn("thinking", g.text)

    async def test_request_shapes_and_auth_headers(self):
        c = self.client()
        await c.generate(CATALOG.get("openai:gpt-6.1-sol"), REQ)
        await c.generate(CATALOG.get("anthropic:claude-sonnet-5-5"), REQ)
        await c.generate(CATALOG.get("gemini:gemini-4-argon"), REQ)
        o, a, g = [r for r in self.srv.requests if r["method"] == "POST"]
        self.assertEqual(o["headers"]["authorization"], "Bearer " + KEYS["openai"])
        self.assertIs(o["json"]["store"], False)
        self.assertEqual(o["json"]["max_output_tokens"], 256)
        self.assertEqual(a["headers"]["x-api-key"], KEYS["anthropic"])
        self.assertEqual(a["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(a["json"]["max_tokens"], 256)
        self.assertNotIn("temperature", a["json"])
        self.assertEqual(g["headers"]["x-goog-api-key"], KEYS["gemini"])
        self.assertEqual(g["json"]["generationConfig"]["maxOutputTokens"], 256)
        self.assertTrue(g["path"].endswith("/models/gemini-4-argon:generateContent"))
        # key never in URL or body
        for r in (o, a, g):
            self.assertNotIn(KEYS[r["provider"]], r["path"])
            self.assertNotIn(KEYS[r["provider"]], str(r["json"]))

    async def test_native_search_citations(self):
        c = self.client()
        req = ProviderRequest("s", "q", 256, native_search=True)
        o = await c.generate(CATALOG.get("openai:gpt-6.1-sol"), req)
        a = await c.generate(CATALOG.get("anthropic:claude-sonnet-5-5"), req)
        g = await c.generate(CATALOG.get("gemini:gemini-4-argon"), req)
        for r, url in ((o, "https://example.org/a"), (a, "https://example.org/b"), (g, "https://example.org/c")):
            self.assertEqual([x.url for x in r.citations][0], url)
            self.assertEqual(r.usage.search_calls, 1)

    async def test_retry_on_429_then_success_honours_retry_after(self):
        self.srv.push("openai", ("status", 429, {"error": {"code": "rate_limit_exceeded", "message": "slow"}}, {"Retry-After": "0.02"}))
        r = await self.client().generate(CATALOG.get("openai:gpt-6.1-sol"), REQ)
        self.assertEqual([a["outcome"] for a in r.attempts], ["error", "ok"])
        self.assertEqual(r.attempts[0]["category"], "rate_limit")
        self.assertEqual(r.retries, 1)
        self.assertEqual(self.srv.count("openai"), 2)

    async def test_persistent_5xx_fails_after_bounded_retries(self):
        self.srv.push("anthropic", *[("status", 500, {"type": "error", "error": {"type": "api_error", "message": "boom"}})] * 5)
        with self.assertRaises(ProviderError) as cm:
            await self.client().generate(CATALOG.get("anthropic:claude-sonnet-5-5"), REQ)
        self.assertEqual(cm.exception.category, "server")
        self.assertEqual(len(cm.exception.attempts), 3)           # 1 + max_retries(2)
        self.assertEqual(self.srv.count("anthropic"), 3)

    async def test_529_is_overloaded_and_retryable(self):
        self.srv.push("anthropic", ("status", 529, {"type": "error", "error": {"type": "overloaded_error", "message": "o"}}))
        r = await self.client().generate(CATALOG.get("anthropic:claude-sonnet-5-5"), REQ)
        self.assertEqual(r.attempts[0]["category"], "overloaded")

    async def test_auth_error_not_retried(self):
        self.srv.push("gemini", ("status", 403, {"error": {"status": "PERMISSION_DENIED", "message": "bad key"}}))
        with self.assertRaises(ProviderError) as cm:
            await self.client().generate(CATALOG.get("gemini:gemini-4-argon"), REQ)
        self.assertEqual(cm.exception.category, "auth")
        self.assertEqual(self.srv.count("gemini"), 1)

    async def test_bad_request_and_quota_not_retried(self):
        self.srv.push("openai", ("status", 400, {"error": {"code": "invalid_request_error", "message": "bad"}}))
        with self.assertRaises(ProviderError) as cm:
            await self.client().generate(CATALOG.get("openai:gpt-6.1-sol"), REQ)
        self.assertEqual(cm.exception.category, "bad_request")
        self.srv.push("openai", ("status", 429, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}))
        with self.assertRaises(ProviderError) as cm:
            await self.client().generate(CATALOG.get("openai:gpt-6.1-sol"), REQ)
        self.assertEqual(cm.exception.category, "quota")
        self.assertEqual(self.srv.count("openai"), 2)

    async def test_model_not_found(self):
        self.srv.push("xai", ("status", 404, {"error": "model not found"}))
        with self.assertRaises(ProviderError) as cm:
            await self.client().generate(CATALOG.get("xai:grok-4.7"), REQ)
        self.assertEqual(cm.exception.category, "model_not_found")

    async def test_timeout_then_retry_succeeds(self):
        self.srv.push("openai", ("sleep", 1.0))
        r = await self.client(timeout_seconds=0.3).generate(CATALOG.get("openai:gpt-6.1-sol"), REQ)
        self.assertEqual(r.attempts[0]["outcome"], "timeout")
        self.assertEqual(r.attempts[1]["outcome"], "ok")

    async def test_slow_drip_server_cannot_exceed_deadline(self):
        self.srv.push("openai", *[("drip", 5)] * 4)
        t = time.monotonic()
        with self.assertRaises(ProviderError) as cm:
            await self.client(timeout_seconds=0.4, deadline_seconds=1.0).generate(CATALOG.get("openai:gpt-6.1-sol"), REQ)
        self.assertEqual(cm.exception.category, "timeout")
        self.assertLess(time.monotonic() - t, 2.0)

    async def test_malformed_json_is_parse_error(self):
        self.srv.push("openai", ("raw", b"{not json"))
        with self.assertRaises(ProviderError) as cm:
            await self.client(max_retries=0).generate(CATALOG.get("openai:gpt-6.1-sol"), REQ)
        self.assertEqual(cm.exception.category, "parse")

    async def test_unexpected_shape_is_parse_error(self):
        self.srv.push("anthropic", ("body", {"hello": "world"}))
        with self.assertRaises(ProviderError) as cm:
            await self.client(max_retries=0).generate(CATALOG.get("anthropic:claude-sonnet-5-5"), REQ)
        self.assertEqual(cm.exception.category, "parse")

    async def test_redirect_is_not_followed_and_key_not_forwarded(self):
        other = FakeProviderServer().start()
        self.addCleanup(other.stop)
        self.srv.push("openai", ("redirect", other.base + "/v1/responses"))
        with self.assertRaises(ProviderError) as cm:
            await self.client(max_retries=0).generate(CATALOG.get("openai:gpt-6.1-sol"), REQ)
        self.assertEqual(cm.exception.category, "network")
        self.assertEqual(len(other.requests), 0)

    async def test_oversized_response_rejected(self):
        self.srv.push("openai", ("big", 500_000))
        with self.assertRaises(ProviderError) as cm:
            await self.client(max_retries=0).generate(CATALOG.get("openai:gpt-6.1-sol"), REQ)
        self.assertEqual(cm.exception.category, "network")

    async def test_unconfigured_provider(self):
        s = make_settings(self.srv)
        s2 = type(s)(**{**s.__dict__, "keys": {"openai": KEYS["openai"]}})
        with self.assertRaises(ProviderError) as cm:
            await ProviderClient(s2).generate(CATALOG.get("xai:grok-4.7"), REQ)
        self.assertEqual(cm.exception.category, "config")
        self.assertEqual(len(self.srv.requests), 0)

    async def test_circuit_breaker_opens_then_recovers(self):
        self.srv.push("xai", *[("status", 503, {"error": "down"})] * 20)
        c = self.client(max_retries=0)
        e = CATALOG.get("xai:grok-4.7")
        for _ in range(3):
            with self.assertRaises(ProviderError):
                await c.generate(e, REQ)
        n = self.srv.count("xai")
        with self.assertRaises(ProviderError) as cm:
            await c.generate(e, REQ)
        self.assertEqual(cm.exception.category, "circuit_open")
        self.assertEqual(self.srv.count("xai"), n)            # no network call while open
        self.assertEqual(c.circuit_states()["xai"], "open")
        self.srv.script["xai"].clear()
        await asyncio.sleep(0.35)
        self.assertEqual(c.circuit_states()["xai"], "half_open")
        r = await c.generate(e, REQ)                           # probe succeeds
        self.assertEqual(c.circuit_states()["xai"], "closed")
        self.assertTrue(r.text)

    async def test_non_transient_errors_do_not_trip_breaker(self):
        self.srv.push("openai", *[("status", 400, {"error": {"message": "x"}})] * 10)
        c = self.client(max_retries=0)
        for _ in range(5):
            with self.assertRaises(ProviderError):
                await c.generate(CATALOG.get("openai:gpt-6.1-sol"), REQ)
        self.assertEqual(c.circuit_states()["openai"], "closed")

    async def test_concurrency_limit_per_provider(self):
        peak = {"cur": 0, "max": 0}
        orig = self.srv.answer_fn

        def slow(provider, req):
            return orig(provider, req)
        self.srv.push("openai", *[("sleep", 0.15)] * 6)
        c = self.client(per_provider_concurrency=2)
        t = time.monotonic()
        rs = await asyncio.gather(*(c.generate(CATALOG.get("openai:gpt-6.1-sol"), REQ) for _ in range(6)))
        self.assertEqual(len(rs), 6)
        # 6 calls x 0.15s with 2 slots >= 0.45s
        self.assertGreaterEqual(time.monotonic() - t, 0.42)

    async def test_truncation_and_refusal_parsing(self):
        self.srv.push("openai", ("body", {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                                          "output": [], "usage": {"input_tokens": 5, "output_tokens": 256}}))
        r = await self.client().generate(CATALOG.get("openai:gpt-6.1-sol"), REQ)
        self.assertTrue(r.truncated)
        self.assertEqual(r.finish_reason, "length")
        self.assertEqual(r.text, "")
        self.srv.push("anthropic", ("body", {"content": [{"type": "text", "text": "I can't help"}], "stop_reason": "refusal",
                                             "usage": {"input_tokens": 5, "output_tokens": 5}}))
        r = await self.client().generate(CATALOG.get("anthropic:claude-sonnet-5-5"), REQ)
        self.assertEqual(r.finish_reason, "refusal")
        self.srv.push("gemini", ("body", {"promptFeedback": {"blockReason": "SAFETY"}}))
        with self.assertRaises(ProviderError) as cm:
            await self.client().generate(CATALOG.get("gemini:gemini-4-argon"), REQ)
        self.assertEqual(cm.exception.category, "content_blocked")

    async def test_anthropic_usage_includes_cache_tokens_and_xai_reported_cost(self):
        self.srv.push("anthropic", ("body", {"content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn",
                                             "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5}}))
        r = await self.client().generate(CATALOG.get("anthropic:claude-sonnet-5-5"), REQ)
        self.assertEqual(r.usage.input_tokens, 100)
        self.assertEqual(r.usage.cached_input_tokens, 90)
        self.srv.push("xai", ("body", {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "x"}]}],
                                       "usage": {"input_tokens": 10, "output_tokens": 5, "cost_in_usd_ticks": 123456789}}))
        r = await self.client().generate(CATALOG.get("xai:grok-4.7"), REQ)
        self.assertAlmostEqual(r.usage.provider_cost_usd, 0.0123456789)

    async def test_discovery(self):
        self.srv.models["openai"] = ["gpt-6.1-sol"]
        self.srv.models["gemini"] = ["gemini-4-argon"]
        c = self.client()
        self.assertEqual(await c.list_models("openai"), {"gpt-6.1-sol"})
        self.assertEqual(await c.list_models("gemini"), {"gemini-4-argon"})

    async def test_metrics_and_prometheus(self):
        c = self.client()
        await c.generate(CATALOG.get("openai:gpt-6.1-sol"), REQ)
        snap = c.metrics.snapshot()
        self.assertEqual(snap["models"]["openai:gpt-6.1-sol"]["ok"], 1)
        text = c.metrics.prometheus(c.circuit_states())
        self.assertIn('atlas_provider_calls_total{provider="openai"', text)
        self.assertNotIn(KEYS["openai"], text)


if __name__ == "__main__":
    unittest.main()
