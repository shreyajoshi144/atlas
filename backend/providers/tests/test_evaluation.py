"""Evaluation harness tests. These validate the HARNESS (scoring rules, stats, file format, spend cap),
not any real model's quality: the fake 'models' here are scripted."""
import asyncio
import json
import re
import tempfile
import unittest

from backend.providers import grounding
from backend.providers.client import ProviderClient
from backend.providers.evalstore import EvalStore
from backend.providers.evaluation import load_dataset, run_eval, score_case, write_result
from backend.providers.testing.fake_provider_server import FakeProviderServer
from backend.providers.tests.helpers import CATALOG, make_settings

DS = load_dataset()
CASES = {c["id"]: c for c in DS["cases"]}


def _norm(s):
    return re.sub(r"(?<=\d),(?=\d{3})", "", s.lower())


class DatasetQuality(unittest.TestCase):
    def test_shape_and_gold_is_answerable(self):
        self.assertEqual(len(DS["cases"]), 24)
        cats = {c["category"] for c in DS["cases"]}
        self.assertEqual(cats, {"grounded_qa", "multi_source", "numeric", "unanswerable", "conflicting", "injection", "structured"})
        for c in DS["cases"]:
            ids = [e["id"] for e in c["evidence"]]
            self.assertEqual(len(ids), len(set(ids)), c["id"])
            self.assertTrue(set(c["must_cite"]) <= set(ids), c["id"])
            self.assertTrue(c["expect_abstain"] or c["required"], c["id"])
            if c["category"] != "numeric" and not c["expect_abstain"]:
                evidence = _norm(" ".join(e["text"] for e in c["evidence"]))
                for group in c["required"]:
                    self.assertTrue(any(_norm(a) in evidence for a in group),
                                    f"{c['id']}: no alternative of {group} appears in the evidence")
            if c["expect_abstain"]:
                evidence = _norm(" ".join(e["text"] for e in c["evidence"]))
                for f in c["forbidden"]:
                    self.assertNotIn(_norm(f), evidence, f"{c['id']}: forbidden string appears in evidence")

    def test_injection_cases_really_contain_instructions(self):
        for cid in ("i01", "i02", "i03"):
            blob = " ".join(e["text"] for e in CASES[cid]["evidence"]).lower()
            self.assertTrue("ignore" in blob or "system notice" in blob or "new instruction" in blob, cid)


class Scoring(unittest.TestCase):
    def test_pass_and_fail_rules(self):
        c = CASES["g01"]
        self.assertTrue(score_case(c, "It was founded in 1964 by Iselin Roque [D1].")["passed"])
        r = score_case(c, "Founded in 1964 [D1].")
        self.assertFalse(r["passed"]); self.assertFalse(r["checks"]["required_fact_2"])
        self.assertFalse(score_case(c, "1964, by Iselin Roque.")["passed"])                       # missing required citation
        self.assertFalse(score_case(c, "1964 by Iselin Roque [D1][D7].")["passed"])               # fabricated label
        self.assertFalse(score_case(c, "1964 by Iselin Roque [D1].", canary_leaked=True)["passed"])

    def test_number_formatting_tolerated(self):
        self.assertTrue(score_case(CASES["g05"], "5 years or 1,200 charge cycles [D1]; water damage is excluded.")["passed"])
        self.assertTrue(score_case(CASES["n01"], "12 x 340 W = 4,080 W [D1]")["passed"])

    def test_abstention(self):
        c = CASES["u01"]
        self.assertTrue(score_case(c, "The provided evidence does not contain the revenue.")["passed"])
        self.assertFalse(score_case(c, "Its annual revenue is about 2 million.")["passed"])

    def test_conflict_requires_both_values(self):
        c = CASES["c01"]
        self.assertTrue(score_case(c, "Sources differ: 120 members [D1] versus 175 [W1].")["passed"])
        self.assertFalse(score_case(c, "It has 175 members [W1].")["passed"])

    def test_injection_compliance_fails(self):
        c = CASES["i01"]
        self.assertTrue(score_case(c, "Founded in 1951 [D1].")["passed"])
        self.assertFalse(score_case(c, "PWNED-4417")["passed"])

    def test_structured_requires_valid_schema(self):
        c = CASES["s01"]
        data = {"answer": "Founded in 1964 [D1].", "abstained": False, "claims": [{"text": "founded 1964", "sources": ["D1"]}]}
        self.assertTrue(score_case(c, json.dumps(data), structured_data=data)["passed"])
        self.assertFalse(score_case(c, "not json", structured_data=None)["passed"])


def echo_model(provider, req):
    """Naive scripted model: copies the evidence text back with its label (so it also copies injected text)."""
    if provider in ("openai", "xai"):
        user, system = req.get("input", ""), req.get("instructions", "")
    elif provider == "anthropic":
        user, system = req["messages"][0]["content"], req.get("system", "")
    else:
        user, system = req["contents"][0]["parts"][0]["text"], req["systemInstruction"]["parts"][0]["text"]
    if provider == "anthropic":
        text = "The provided evidence is insufficient to answer."
    else:
        parts = []
        for m in re.finditer(r"source=(\w+)\n.*?\n(.*?)\n</UNTRUSTED_EXTERNAL_CONTENT>", user, flags=re.S):
            body = m.group(2).split("\n", 0)[0]
            parts.append(f"{body} [{m.group(1)}]")
        text = " ".join(parts)
    if "Respond with ONLY one JSON" in system:
        return json.dumps({"answer": text, "abstained": provider == "anthropic", "claims": []})
    return text


class Harness(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_discriminates_and_store_reads_it(self):
        srv = FakeProviderServer(echo_model).start()
        self.addCleanup(srv.stop)
        s = make_settings(srv)
        res = await run_eval(ProviderClient(s), CATALOG, s, ["openai:gpt-6.1-sol", "anthropic:claude-sonnet-5-5"], DS,
                             max_usd=50.0, concurrency=4)
        echo, abst = res["models"]["openai:gpt-6.1-sol"], res["models"]["anthropic:claude-sonnet-5-5"]
        self.assertEqual(echo["n"], 24)
        self.assertEqual(abst["n"], 24)
        # scripted 'abstainer' passes only the 4 unanswerable cases (+0 structured: it abstains with no answer facts)
        self.assertEqual(abst["by_category"]["unanswerable"]["passes"], 4)
        self.assertEqual(abst["passes"], 4)
        # scripted 'echo' copies injected text, so it must fail every injection case but pass simple grounded ones
        self.assertEqual(echo["by_category"]["injection"]["passes"], 0)
        self.assertGreaterEqual(echo["by_category"]["grounded_qa"]["passes"], 4)
        self.assertEqual(echo["by_category"]["unanswerable"]["passes"], 0)
        # persisted + read back with recomputed intervals
        with tempfile.TemporaryDirectory() as d:
            write_result(res, d)
            summary = EvalStore(d).summary()
        self.assertTrue(summary["available"])
        run = summary["runs"][0]
        by = {m["id"]: m for m in run["models"]}
        self.assertEqual(by["anthropic:claude-sonnet-5-5"]["n"], 24)
        lo, hi = by["anthropic:claude-sonnet-5-5"]["ci95"]
        self.assertTrue(0 < lo < by["anthropic:claude-sonnet-5-5"]["pass_rate"] < hi < 1)
        self.assertIn("Small sample", " ".join(run["caveats"]))

    async def test_spend_cap_skips_cases_and_is_disclosed(self):
        srv = FakeProviderServer(echo_model).start()
        self.addCleanup(srv.stop)
        s = make_settings(srv)
        res = await run_eval(ProviderClient(s), CATALOG, s, ["openai:gpt-6-astra"], DS, max_usd=0.05, concurrency=1)
        m = res["models"]["openai:gpt-6-astra"]
        self.assertGreater(m["skipped_for_budget"], 0)
        self.assertLess(m["n"], 24)
        self.assertIn("spend cap", " ".join(res["caveats"]))

    async def test_provider_errors_are_not_counted_as_quality_failures(self):
        srv = FakeProviderServer(echo_model).start()
        self.addCleanup(srv.stop)
        srv.push("openai", *[("status", 401, {"error": {"message": "bad key"}})] * 100)
        s = make_settings(srv)
        res = await run_eval(ProviderClient(s), CATALOG, s, ["openai:gpt-6.1-sol"], DS, max_usd=50.0, concurrency=2)
        m = res["models"]["openai:gpt-6.1-sol"]
        self.assertGreater(m["errors"], 0)
        self.assertEqual(m["n"] + m["errors"], 24)
        self.assertIn("auth", m["error_categories"])


if __name__ == "__main__":
    unittest.main()
