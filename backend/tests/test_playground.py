from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.tests.test_m9_service_api import BIG, SMALL, activate, body, make_policy, repository, script, world

BASE = "/api/playground"


def test_playground_endpoints_require_authentication(world):
    client = world["c"]
    assert client.get(f"{BASE}/models").status_code == 401
    assert client.get(f"{BASE}/conversations").status_code == 401
    assert client.post(f"{BASE}/chat", json={"message": "Compare evidence", "models": [BIG]}).status_code == 401


def test_manual_model_comparison_uses_router_and_persists_provenance(world, monkeypatch):
    client = world["c"]
    models = body(client.get(f"{BASE}/models", headers=world["ha"]))["models"]
    assert {item["model"] for item in models} >= {BIG, SMALL}
    policy = make_policy(world, model=BIG)
    activate(world, policy["policy_id"])
    script(world, SMALL, [("ok", 19, 11)])
    script(world, BIG, [("ok", 23, 13)])

    from backend.api.routes import chat as chat_route
    used_user_ids = []
    class Memory:
        class Collection:
            def count(self): return 3
        collection = Collection()
    def retrieve(_memory, topic, question, history, top_k, user_id):
        used_user_ids.append(user_id)
        return {"retrieved_chunks": [{"topic": "Case evidence", "content": "A supported document fact.",
                                      "report_id": "report_owned", "chunk_id": "chunk_1", "similarity_score": 0.91}],
                "report_ids": ["report_owned"]}
    monkeypatch.setattr(chat_route, "_vector_memory", lambda: Memory())
    monkeypatch.setattr(chat_route, "_retrieve_context", retrieve)

    result = body(client.post(f"{BASE}/chat", headers=world["ha"], json={
        "message": "Compare this evidence", "models": [SMALL, BIG], "include_documents": True,
        "include_web": False, "top_k": 4,
    }))
    assert {item["model"] for item in result["outputs"]} == {SMALL, BIG}
    assert all(item["status"] == "ok" and item["provider"] == "Groq" for item in result["outputs"])
    assert all(item["usage"]["total_tokens"] in (30, 36) for item in result["outputs"])
    assert {item["id"] for item in result["sources"]} == {"D1"}
    assert used_user_ids == [world["ua"]]
    assert result["conversation_id"] and result["messages"][-1]["outputs"]
    rows, total = world["router"].repos()[0].list_usage(world["ua"], purpose="chat", limit=20)
    manual = [row for row in rows if row["reason_code"] == "playground_manual"]
    assert total >= 2 and {row["actual_model"] for row in manual} >= {SMALL, BIG}
    assert all(row["fallback_used"] == 0 for row in manual)
    assert body(client.get("/api/routing/policies", headers=world["ha"]))["active_policy_id"] == policy["policy_id"]


def test_playground_conversations_are_isolated_and_deletable(world):
    client = world["c"]
    alice = body(client.post(f"{BASE}/chat", headers=world["ha"], json={
        "message": "A private playground question", "models": [BIG], "include_documents": False,
    }))
    conversation_id = alice["conversation_id"]
    assert client.get(f"{BASE}/conversations/{conversation_id}", headers=world["hb"]).status_code == 404
    assert body(client.get(f"{BASE}/conversations", headers=world["hb"]))["items"] == []
    assert client.delete(f"{BASE}/conversations/{conversation_id}", headers=world["hb"]).status_code == 404
    opened = body(client.get(f"{BASE}/conversations/{conversation_id}", headers=world["ha"]))
    assert opened["messages"][0]["content"] == "A private playground question"
    assert body(client.delete(f"{BASE}/conversations/{conversation_id}", headers=world["ha"]))["conversation_id"] == conversation_id


def test_playground_chat_does_not_recreate_conversation_deleted_during_provider_call(world, monkeypatch):
    client = world["c"]
    original = body(client.post(f"{BASE}/chat", headers=world["ha"], json={
        "message": "Create a conversation to exercise a save race", "models": [BIG], "include_documents": False,
    }))
    conversation_id = original["conversation_id"]

    from backend.api.routes import playground as playground_route

    def delete_during_provider_call(user_id, model, system_prompt, user_prompt):
        assert playground_route._repo().delete(user_id, conversation_id)
        return {"model": model, "provider": "Groq", "status": "ok", "answer": "late answer",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
                          "estimated_cost_usd": 0.0, "actual_cost_usd": 0.0, "latency_ms": 1}}

    monkeypatch.setattr(playground_route, "_invoke_model", delete_during_provider_call)
    response = client.post(f"{BASE}/chat", headers=world["ha"], json={
        "message": "This response must not recreate the deleted conversation", "models": [BIG],
        "conversation_id": conversation_id, "include_documents": False,
    })
    assert response.status_code == 409
    assert response.json()["detail"] == "This conversation was deleted before the response could be saved."
    assert client.get(f"{BASE}/conversations/{conversation_id}", headers=world["ha"]).status_code == 404
    items = body(client.get(f"{BASE}/conversations", headers=world["ha"]))["items"]
    assert conversation_id not in {item["conversation_id"] for item in items}


def test_playground_rejects_unknown_or_duplicate_models(world):
    client = world["c"]
    assert client.post(f"{BASE}/chat", headers=world["ha"], json={
        "message": "Select a model", "models": ["unconfigured-model"], "include_documents": False,
    }).status_code == 400
    assert client.post(f"{BASE}/chat", headers=world["ha"], json={
        "message": "Select a model", "models": [BIG, BIG], "include_documents": False,
    }).status_code == 422


def test_playground_daily_limit_blocks_provider_call(world):
    from backend.ops.repository import OperationalRepository

    OperationalRepository(world["repo"].db).set_limits(world["ua"], daily_call_limit=0,
                                                         monthly_spend_cap_usd=None, actor_id=world["ua"])
    calls_before = len(world["calls"])
    response = body(world["c"].post(f"{BASE}/chat", headers=world["ha"], json={
        "message": "This call should be blocked", "models": [BIG], "include_documents": False,
    }))
    assert response["outputs"][0]["status"] == "blocked"
    assert "Daily" in response["outputs"][0]["error"]
    assert len(world["calls"]) == calls_before


def test_playground_monthly_spend_cap_blocks_before_provider_call(world):
    from backend.ops.repository import OperationalRepository

    OperationalRepository(world["repo"].db).set_limits(world["ua"], daily_call_limit=10,
                                                         monthly_spend_cap_usd=0, actor_id=world["ua"])
    calls_before = len(world["calls"])
    response = body(world["c"].post(f"{BASE}/chat", headers=world["ha"], json={
        "message": "This call exceeds the spend cap", "models": [BIG], "include_documents": False,
    }))
    assert response["outputs"][0]["status"] == "blocked"
    assert "Monthly" in response["outputs"][0]["error"]
    assert len(world["calls"]) == calls_before


def test_playground_partial_provider_failure_is_reported_without_losing_success(world):
    from backend.tests.test_m9_router import Http, ok

    script(world, SMALL, [Http(503)])
    script(world, BIG, [ok(10, 4)])
    response = body(world["c"].post(f"{BASE}/chat", headers=world["ha"], json={
        "message": "Compare available model responses", "models": [SMALL, BIG], "include_documents": False,
    }))
    by_model = {item["model"]: item for item in response["outputs"]}
    assert by_model[SMALL]["status"] == "failed" and by_model[BIG]["status"] == "ok"
    assert response["messages"][-1]["outputs"] == response["outputs"]


def test_playground_web_search_failure_is_explicit_and_does_not_block_chat(world, monkeypatch):
    from backend.services import tavily_service
    class BrokenSearch:
        def __init__(self): pass
        def search(self, *args, **kwargs): raise RuntimeError("provider secret must not escape")
    monkeypatch.setattr(tavily_service, "TavilySearchService", BrokenSearch)
    result = body(world["c"].post(f"{BASE}/chat", headers=world["ha"], json={
        "message": "Answer if possible", "models": [BIG], "include_documents": False, "include_web": True,
    }))
    assert result["outputs"][0]["status"] == "ok"
    assert result["warnings"] == ["Web search was unavailable; models answered using the remaining context."]


def test_playground_web_results_are_returned_as_citable_sources(world, monkeypatch):
    from backend.services import tavily_service
    class Search:
        def __init__(self): pass
        def search(self, *args, **kwargs):
            return [SimpleNamespace(title="Research paper", url="https://example.org/paper", snippet="A source excerpt.",
                                    domain="example.org", published_date="2026-01-01")]
    monkeypatch.setattr(tavily_service, "TavilySearchService", Search)
    result = body(world["c"].post(f"{BASE}/chat", headers=world["ha"], json={
        "message": "Summarize this paper", "models": [BIG], "include_documents": False, "include_web": True,
    }))
    assert result["sources"] == [{"id": "W1", "kind": "web", "title": "Research paper",
                                  "url": "https://example.org/paper", "excerpt": "A source excerpt.",
                                  "domain": "example.org", "published_date": "2026-01-01"}]


def test_playground_frontend_is_integrated_and_escapes_dynamic_content():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    html = (root / "frontend/app.html").read_text()
    js = (root / "frontend/js/playground.js").read_text()
    css = (root / "frontend/css/atlas.css").read_text()
    assert 'data-nav="playground"' in html and 'src="js/playground.js"' in html
    for label in ("AI Playground", "Select up to 3", "My research library", "Web search", "Evidence provided"):
        assert label in js
    assert "escapeHtml" in js and "escapeAttr" in js and "localStorage" not in js
    assert "/playground/chat" in js and "/playground/conversations" in js
    assert ".pg-layout" in css and "@media (max-width: 620px)" in css
