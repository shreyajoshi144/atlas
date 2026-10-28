"""Handler-level tests for /api/compare (see route_stub.py for what is and is not covered)."""
import inspect
import json
import logging
import unittest

from backend.providers.tests import app_stub
app_stub.install()
from backend.providers.tests import route_stub
STUBBED = route_stub.install()

from backend.api.routes import compare as routes
from backend.auth.dependencies import get_current_user, require_admin_user
from backend.providers.client import ProviderClient
from backend.providers.discovery import DiscoveryCache
from backend.providers.evalstore import EvalStore
from backend.providers.repository import CompareRepository
from backend.providers.service import CompareService, OperationalBudget
from backend.providers.testing.fake_provider_server import FakeProviderServer
from backend.providers.tests.helpers import CATALOG, KEYS, make_settings

try:
    from fastapi import HTTPException
except Exception:  # pragma: no cover
    raise
U1, U2 = {"user_id": "u1", "role": "user"}, {"user_id": "u2", "role": "user"}


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.srv = FakeProviderServer().start()
        self.addCleanup(self.srv.stop)
        self.db = app_stub.make_db()
        s = make_settings(self.srv)
        client = ProviderClient(s)
        self.svc = CompareService(settings=s, client=client, catalog=CATALOG, repo=CompareRepository(self.db),
                                  budget=OperationalBudget(self.db, lambda: 1000, lambda: None),
                                  discovery=DiscoveryCache(client, CATALOG, s), eval_store=EvalStore("/nonexistent"))
        routes.reset_service_for_tests(self.svc)
        self.addCleanup(routes.reset_service_for_tests, None)

    def body(self, **kw):
        d = dict(prompt="What is a tide?", models=["openai:gpt-6.1-sol", "anthropic:claude-sonnet-5-5"], consent_external=True)
        d.update(kw)
        return routes.RunBody(**d)

    # ---- authentication / authorization wiring ----
    def test_every_handler_requires_a_user_and_admin_routes_require_admin(self):
        handlers = [n for n, f in vars(routes).items() if inspect.iscoroutinefunction(f) and f.__module__ == routes.__name__
                    and n not in ("_evidence",)]
        self.assertGreaterEqual(len(handlers), 10)
        for name in handlers:
            params = inspect.signature(getattr(routes, name)).parameters
            deps = [p.default.dependency for p in params.values() if hasattr(p.default, "dependency")]
            self.assertTrue(get_current_user in deps or require_admin_user in deps, f"{name} has no auth dependency")
            if name.startswith("admin_"):
                self.assertIn(require_admin_user, deps, name)

    def test_request_model_limits(self):
        with self.assertRaises(Exception):
            routes.RunBody(prompt="hi there", models=["a", "b", "c", "d", "e"])
        with self.assertRaises(Exception):
            routes.AskBody(prompt="hi there", models=["a", "b", "c", "d"])
        with self.assertRaises(Exception):
            routes.RunBody(prompt="hi there", models=["a"], evidence_mode="everything")
        self.assertFalse(routes.RunBody(prompt="hi there", models=["a"]).consent_external)   # consent is opt-in

    # ---- behaviour ----
    async def test_run_success_envelope(self):
        out = await routes.run_compare(self.body(), current_user=U1)
        self.assertTrue(out.success)
        self.assertEqual(len(out.data["results"]), 2)
        self.assertTrue(all(r["status"] == "ok" for r in out.data["results"]))

    async def test_compare_errors_map_to_http_status(self):
        with self.assertRaises(HTTPException) as cm:
            await routes.run_compare(self.body(consent_external=False), current_user=U1)
        self.assertEqual(cm.exception.status_code, 400)
        with self.assertRaises(HTTPException) as cm:
            await routes.run_compare(self.body(models=["openai:not-a-real-model"]), current_user=U1)
        self.assertEqual(cm.exception.status_code, 400)
        self.svc.s.__class__  # settings is frozen; use a tiny ceiling via a fresh service
        s2 = make_settings(self.srv, max_cost_per_request_usd=0.0001)
        svc2 = CompareService(settings=s2, client=ProviderClient(s2), catalog=CATALOG, repo=self.svc.repo, budget=self.svc.budget)
        routes.reset_service_for_tests(svc2)
        with self.assertRaises(HTTPException) as cm:
            await routes.run_compare(self.body(), current_user=U1)
        self.assertEqual(cm.exception.status_code, 402)

    async def test_account_budget_maps_to_429(self):
        svc = CompareService(settings=self.svc.s, client=self.svc.client, catalog=CATALOG, repo=self.svc.repo,
                             budget=OperationalBudget(self.db, lambda: 0, lambda: None))
        routes.reset_service_for_tests(svc)
        with self.assertRaises(HTTPException) as cm:
            await routes.run_compare(self.body(), current_user=U1)
        self.assertEqual(cm.exception.status_code, 429)

    async def test_unexpected_error_is_generic_and_leaks_nothing(self):
        class Boom:
            async def run(self, *a, **k):
                raise RuntimeError("secret-xyz sk-live-abcdef123456")
        routes.reset_service_for_tests(Boom())
        with self.assertRaises(HTTPException) as cm:
            await routes.run_compare(self.body(), current_user=U1)
        self.assertEqual(cm.exception.status_code, 500)
        self.assertNotIn("secret-xyz", json.dumps(cm.exception.detail))

    async def test_runs_are_owner_scoped(self):
        out = await routes.run_compare(self.body(), current_user=U1)
        rid = out.data["run_id"]
        self.assertEqual((await routes.get_run(rid, current_user=U1)).data["run_id"], rid)
        with self.assertRaises(HTTPException) as cm:
            await routes.get_run(rid, current_user=U2)
        self.assertEqual(cm.exception.status_code, 404)
        with self.assertRaises(HTTPException) as cm:
            await routes.delete_run(rid, current_user=U2)
        self.assertEqual(cm.exception.status_code, 404)
        self.assertEqual((await routes.list_runs(current_user=U2)).data["items"], [])
        self.assertEqual(len((await routes.list_runs(current_user=U1)).data["items"]), 1)
        await routes.delete_run(rid, current_user=U1)
        with self.assertRaises(HTTPException):
            await routes.get_run(rid, current_user=U1)

    async def test_no_provider_key_in_any_response(self):
        out = await routes.run_compare(self.body(), current_user=U1)
        blobs = [json.dumps(out.data), json.dumps((await routes.list_models(current_user=U1)).data),
                 json.dumps((await routes.admin_health(admin=U1)).data),
                 json.dumps((await routes.admin_metrics(format="json", admin=U1)).data),
                 routes_text(await routes.admin_metrics(format="prometheus", admin=U1))]
        for blob in blobs:
            for k in KEYS.values():
                self.assertNotIn(k, blob)

    async def test_evaluations_say_nothing_when_none_exist(self):
        out = await routes.evaluations(current_user=U1)
        self.assertFalse(out.data["available"])
        run = await routes.run_compare(self.body(), current_user=U1)
        self.assertFalse(run.data["evaluation"]["available"])
        self.assertIn("cannot say which one is more accurate", run.data["evaluation"]["message"])

    async def test_admin_discover(self):
        self.srv.models["openai"] = ["gpt-6.1-sol"]
        out = await routes.admin_discover(admin=U1)
        self.assertEqual(out.data["openai"]["status"], "ok")
        self.assertIn("gpt-6-astra", out.data["openai"]["catalog_models_missing"])


def routes_text(resp):
    return getattr(resp, "body", resp) if not isinstance(getattr(resp, "body", None), bytes) else resp.body.decode()


if __name__ == "__main__":
    unittest.main()
