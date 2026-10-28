"""Phase 1C/1D — controlled selection and execution of a ResearchPlan.

This module is deliberately separate from the legacy research graph.  The
existing graph remains the safe fallback while callers can opt into this
controlled loop.  No tool is selected outside ResearchPlan/ToolRegistry and
no callable/function can be supplied by agent-generated data.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, TypedDict

from backend.agentic.common import ToolName
from backend.agentic.planning.models import PlannedAction, ResearchPlan
from backend.agentic.tools.base import ToolResult
from backend.agentic.tools.registry import ToolRegistry, UnknownToolError
from backend.agentic.tools.schemas import (
    RequestMoreEvidenceOutput,
    SearchExistingEvidenceOutput,
    SearchWebOutput,
    ScrapeSourceOutput,
    VerifyClaimOutput,
    EvidenceGap,
)
from backend.core.logging import get_logger
from backend.core.observability import append_trace, classify_error

logger = get_logger(__name__)


class ActionExecutionRecord(TypedDict, total=False):
    action_id: str
    tool: str
    status: str
    duration_ms: int
    result_metadata: dict[str, Any]
    error: Optional[dict[str, Any]]


class AgenticExecutionState(TypedDict, total=False):
    """State owned by the controlled agentic loop.

    ``tool_results`` contains typed ToolResult objects. ``tool_trace`` contains
    metadata only; it intentionally excludes tool payloads and sensitive inputs.
    """

    research_plan: ResearchPlan
    current_action_id: Optional[str]
    completed_action_ids: list[str]
    tool_results: dict[str, ToolResult]
    action_tool_results: dict[str, list[ToolResult]]
    tool_trace: list[ActionExecutionRecord]
    tool_call_count: int
    agentic_status: str
    agentic_error: Optional[str]
    execution_metrics: dict[str, Any]
    verification_status: str
    evidence_gaps: list[EvidenceGap]
    replan_count: int
    replan_history: list[dict[str, Any]]
    newly_discovered_sources: list[Any]
    agentic_trace: list[dict[str, Any]]


@dataclass(frozen=True)
class SelectionDecision:
    action: Optional[PlannedAction]
    reason: str
    terminal: bool = False


class ActionSelector:
    """Select the next executable action from a validated plan.

    Selection is deterministic and conservative: plan order is authoritative,
    dependencies must be completed successfully, and the registry must contain
    the exact planned tool.
    """

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def select_next(
        self,
        plan: ResearchPlan,
        completed_action_ids: set[str],
        successful_action_ids: set[str],
        tool_call_count: int,
        metrics: Mapping[str, Any] | None = None,
        *,
        runtime_actions: Mapping[str, PlannedAction] | None = None,
        forced_action_ids: set[str] | None = None,
    ) -> SelectionDecision:
        if not isinstance(plan, ResearchPlan):
            return SelectionDecision(None, "invalid research plan", terminal=True)

        if tool_call_count >= plan.stopping_criteria.max_tool_calls:
            return SelectionDecision(None, "tool/action budget exhausted", terminal=True)

        metrics = metrics or {}
        completed = set(completed_action_ids)
        successful = set(successful_action_ids)
        runtime = runtime_actions or {}
        forced = forced_action_ids or set()
        actions = [*runtime.values(), *plan.planned_actions]
        # Forced runtime actions are the immediate consequence of a verification
        # gap. They are considered before unrelated planned work so the
        # re-verification chain cannot be bypassed.
        ordered_actions = [a for a in actions if a.action_id in forced] + [
            a for a in actions if a.action_id not in forced
        ]

        # Evidence/source thresholds only tell us that *searching* can stop.
        # They must never bypass mandatory planned actions such as scraping or
        # verification. Always select an eligible required action first.
        for action in ordered_actions:
            if action.action_id in completed:
                continue

            # Plan validation guarantees dependencies exist and are earlier.
            if any(dep not in successful for dep in action.depends_on):
                continue

            try:
                self.registry.get(action.tool)
            except UnknownToolError:
                return SelectionDecision(
                    None,
                    f"planned tool is not registered: {str(action.tool)}",
                    terminal=True,
                )

            # request_more_evidence is conditional by schema. The selector only
            # releases it when the stopping metrics show a gap.
            if action.tool == ToolName.REQUEST_MORE_EVIDENCE and not self._evidence_gap(plan, metrics) and action.action_id not in forced:
                continue

            return SelectionDecision(action, "next action is allowed")

        # No required action is currently executable. Evidence thresholds can
        # now stop further searching, but only after mandatory plan work is
        # complete (or a conditional evidence request is intentionally skipped).
        required_remaining = [
            action for action in ordered_actions
            if action.action_id not in completed
            and action.tool != ToolName.REQUEST_MORE_EVIDENCE
        ]
        if not required_remaining and self._stopping_reached(plan, metrics):
            return SelectionDecision(None, "stopping criteria reached", terminal=True)

        all_action_ids = {a.action_id for a in ordered_actions}
        if all_action_ids.issubset(completed):
            return SelectionDecision(None, "all planned actions completed", terminal=True)

        return SelectionDecision(None, "no action currently has satisfied dependencies", terminal=True)

    @staticmethod
    def _evidence_gap(plan: ResearchPlan, metrics: Mapping[str, Any]) -> bool:
        coverage = float(metrics.get("requirement_coverage", 0.0))
        verified = float(metrics.get("verified_claim_ratio", 0.0))
        source_count = int(metrics.get("source_count", 0))
        return (
            source_count < plan.stopping_criteria.target_source_count
            or coverage < plan.stopping_criteria.min_requirement_coverage
            or verified < plan.stopping_criteria.min_verified_claim_ratio
        )

    @classmethod
    def _stopping_reached(cls, plan: ResearchPlan, metrics: Mapping[str, Any]) -> bool:
        # A stop requires the core source target plus both quality thresholds.
        # Distinct domains is also enforced when it is supplied by execution.
        target = plan.stopping_criteria
        return (
            int(metrics.get("source_count", 0)) >= target.target_source_count
            and int(metrics.get("distinct_domain_count", 0)) >= target.min_distinct_domains
            and float(metrics.get("requirement_coverage", 0.0)) >= target.min_requirement_coverage
            and float(metrics.get("verified_claim_ratio", 0.0)) >= target.min_verified_claim_ratio
        )


class AgenticToolExecutor:
    """Controlled Plan → Select → Execute → Update loop.

    The executor accepts only a validated ResearchPlan and a ToolRegistry.
    ``execution_context`` is trusted caller context (for example the
    authenticated user id); it is never model-generated.
    """

    def __init__(self, registry: ToolRegistry, selector: ActionSelector | None = None):
        self.registry = registry
        self.selector = selector or ActionSelector(registry)

    def run(
        self,
        plan: ResearchPlan,
        *,
        execution_context: Mapping[str, Any] | None = None,
        initial_state: Mapping[str, Any] | None = None,
    ) -> AgenticExecutionState:
        if not isinstance(plan, ResearchPlan):
            return self._failed_state("invalid research plan")

        context = dict(execution_context or {})
        state: AgenticExecutionState = {
            "research_plan": plan,
            "current_action_id": None,
            "completed_action_ids": [],
            "tool_results": {},
            "action_tool_results": {},
            "tool_trace": [],
            "tool_call_count": 0,
            "agentic_status": "running",
            "agentic_error": None,
            "execution_metrics": {},
            "verification_status": "not_run",
            "evidence_gaps": [],
            "replan_count": 0,
            "replan_history": [],
            "newly_discovered_sources": [],
            "agentic_trace": [],
            "research_trace": [],
        }
        if initial_state:
            # Only safe, declared execution-state fields may be carried over.
            for key in ("completed_action_ids", "tool_results", "action_tool_results", "tool_trace", "tool_call_count", "execution_metrics", "research_trace"):
                if key in initial_state:
                    state[key] = initial_state[key]  # type: ignore[literal-required]

        max_calls = plan.stopping_criteria.max_tool_calls
        runtime_actions: dict[str, PlannedAction] = {}
        forced_action_ids: set[str] = set()
        gap_fingerprints: set[str] = set()
        next_action_number = self._next_runtime_action_number(plan)
        while state["tool_call_count"] < max_calls:
            metrics = self._calculate_metrics(plan, state)
            state["execution_metrics"] = metrics
            decision = self.selector.select_next(
                plan,
                set(state["completed_action_ids"]),
                {
                    action_id
                    for action_id, results in state.get("action_tool_results", {}).items()
                    if results and all(result.success for result in results)
                },
                state["tool_call_count"],
                metrics,
                runtime_actions=runtime_actions,
                forced_action_ids=forced_action_ids,
            )

            if decision.action is None:
                state["current_action_id"] = None
                if decision.reason == "tool/action budget exhausted":
                    state["agentic_status"] = "completed"
                elif decision.reason in {"stopping criteria reached", "all planned actions completed"}:
                    state["agentic_status"] = (
                        "completed_with_gaps"
                        if state.get("evidence_gaps") and state.get("verification_status") != "supported"
                        else "completed"
                    )
                else:
                    state["agentic_status"] = "blocked"
                if decision.reason not in {"stopping criteria reached", "all planned actions completed"}:
                    state["agentic_error"] = decision.reason
                return state

            action = decision.action
            state["current_action_id"] = action.action_id
            forced_action_ids.discard(action.action_id)
            state["research_trace"] = append_trace(
                state, component="tool_selection", action="select", status="succeeded",
                metadata={"tool": str(action.tool), "action_id": action.action_id, "reason": decision.reason}
            )

            try:
                self.registry.get(action.tool)
            except UnknownToolError:
                self._record_failure(state, action, "unknown_tool", "Planned tool is not registered.")
                state["agentic_status"] = "failed"
                state["agentic_error"] = "unregistered planned tool"
                return state

            results = self._execute_action(action, state, context)
            state["action_tool_results"][action.action_id] = results
            # Preserve the existing singular typed result surface for callers;
            # evidence/metrics use action_tool_results so no scrape output is lost.
            state["tool_results"][action.action_id] = results[-1]

            expected_scrape_count = (
                len(self._sources_for_scrape_action(action, state))
                if action.tool == ToolName.SCRAPE_SOURCE
                else 1
            )
            action_success = len(results) == expected_scrape_count and all(result.success for result in results)
            # An action is considered completed once its planned attempt has
            # finished. Dependency eligibility still uses successful results,
            # so a failed action can never unlock downstream work. Recording
            # the attempt prevents the same failed action from looping forever.
            if action.action_id not in state["completed_action_ids"]:
                state["completed_action_ids"].append(action.action_id)

            if action.tool == ToolName.REQUEST_MORE_EVIDENCE and results and results[-1].success:
                output = results[-1].output
                if isinstance(output, RequestMoreEvidenceOutput):
                    state["newly_discovered_sources"].extend(output.new_sources)
            if action.action_id in runtime_actions:
                state["agentic_trace"].append({
                    "event": "follow_up_result",
                    "action_id": action.action_id,
                    "tool": str(action.tool),
                    "status": "succeeded" if action_success else "failed",
                })
            if action.tool == ToolName.VERIFY_CLAIM and results and results[-1].success:
                self._process_verification_result(
                    state,
                    results[-1],
                    plan,
                    runtime_actions,
                    forced_action_ids,
                    gap_fingerprints,
                    next_action_number,
                    is_reverification=action.action_id in runtime_actions,
                )
                next_action_number = self._next_runtime_action_number(plan, runtime_actions)
            if not action_success:
                logger.warning(
                    "Agentic tool action failed",
                    extra={"action_id": action.action_id, "tool": str(action.tool), "status": "failed"},
                )

        state["current_action_id"] = None
        state["agentic_status"] = "budget_exhausted"
        return state

    @staticmethod
    def _next_runtime_action_number(
        plan: ResearchPlan,
        runtime_actions: Mapping[str, PlannedAction] | None = None,
    ) -> int:
        ids = [a.action_id for a in plan.planned_actions]
        ids.extend((runtime_actions or {}).keys())
        numbers = [int(action_id.split("_")[1]) for action_id in ids if action_id.startswith("act_") and action_id.split("_")[1].isdigit()]
        return max(numbers, default=0) + 1

    @staticmethod
    def _gap_fingerprint(gap: EvidenceGap) -> str:
        return "|".join((gap.claim.strip().lower(), gap.missing_evidence.strip().lower(), gap.reason.strip().lower()))

    @staticmethod
    def _normalized_verification_status(output: VerifyClaimOutput) -> str:
        if output.assessment is not None:
            return output.assessment.status
        statuses = {str(c.status) for c in output.verification.claims}
        if statuses and statuses.issubset({"verified"}):
            return "supported"
        if "conflicting" in statuses:
            return "conflicting_evidence"
        if "not_verified" in statuses:
            return "unsupported_claim"
        return "insufficient_evidence"

    def _process_verification_result(
        self,
        state: AgenticExecutionState,
        result: ToolResult,
        plan: ResearchPlan,
        runtime_actions: dict[str, PlannedAction],
        forced_action_ids: set[str],
        gap_fingerprints: set[str],
        next_action_number: int,
        *,
        is_reverification: bool = False,
    ) -> None:
        output = result.output
        if not isinstance(output, VerifyClaimOutput):
            return
        assessment_status = self._normalized_verification_status(output)
        state["verification_status"] = assessment_status
        state["evidence_gaps"] = list(output.evidence_gaps)
        state["agentic_trace"].append({
            "event": "verification_result",
            "status": assessment_status,
            "gap_count": len(output.evidence_gaps),
        })
        state["research_trace"] = append_trace(
            state, component="verification", action="verify", status=assessment_status,
            metadata={"gap_count": len(output.evidence_gaps), "reverification": is_reverification}
        )
        if is_reverification:
            state["agentic_trace"].append({"event": "re_verification", "status": assessment_status})
        if assessment_status == "supported" or not output.evidence_gaps:
            return
        if state["replan_count"] >= self._max_replans(plan):
            state["agentic_trace"].append({"event": "replan_created", "status": "max_replans_reached"})
            return

        gap = next((g for g in output.evidence_gaps if self._gap_fingerprint(g) not in gap_fingerprints), None)
        if gap is None:
            state["agentic_trace"].append({"event": "evidence_gap_detected", "status": "duplicate_gap_skipped"})
            return
        fingerprint = self._gap_fingerprint(gap)
        gap_fingerprints.add(fingerprint)
        state["research_trace"] = append_trace(
            state, component="replanning", action="evidence_gap_detected", status="detected",
            metadata={"replan_count": state["replan_count"], "gap_count": len(output.evidence_gaps)}
        )
        preferred_tool = gap.preferred_tool or ToolName.REQUEST_MORE_EVIDENCE.value
        try:
            preferred_tool_name = ToolName(preferred_tool)
            self.registry.get(preferred_tool_name)
        except (ValueError, UnknownToolError):
            state["agentic_trace"].append({
                "event": "replan_created",
                "status": "skipped_unregistered_tool",
            })
            return
        if preferred_tool_name != ToolName.REQUEST_MORE_EVIDENCE:
            state["agentic_trace"].append({
                "event": "replan_created",
                "status": "skipped_unsupported_follow_up_tool",
            })
            return

        replan_no = state["replan_count"] + 1
        base = next_action_number
        search_id, scrape_id, verify_id = f"act_{base}", f"act_{base + 1}", f"act_{base + 2}"
        sq_id = plan.sub_questions[0].sub_question_id
        search_action = PlannedAction(
            action_id=search_id,
            tool=ToolName.REQUEST_MORE_EVIDENCE,
            purpose=f"Target evidence gap for claim: {gap.claim[:180]}",
            sub_question_ids=[sq_id],
            max_results=min(10, 5 + replan_no),
            depends_on=[self._latest_verification_action_id(state)],
            condition="verification_gap",
        )
        scrape_action = PlannedAction(
            action_id=scrape_id,
            tool=ToolName.SCRAPE_SOURCE,
            purpose="Scrape the newly discovered sources for the identified evidence gap.",
            sub_question_ids=[sq_id],
            depends_on=[search_id],
        )
        verify_action = PlannedAction(
            action_id=verify_id,
            tool=ToolName.VERIFY_CLAIM,
            purpose="Re-verify the claim using the newly collected evidence.",
            sub_question_ids=[sq_id],
            depends_on=[scrape_id],
        )
        runtime_actions.update({search_id: search_action, scrape_id: scrape_action, verify_id: verify_action})
        forced_action_ids.add(search_id)
        state["replan_count"] = replan_no
        state["replan_history"].append({
            "replan_count": replan_no,
            "gap": gap.model_dump(mode="json"),
            "actions": [search_id, scrape_id, verify_id],
        })
        state["research_trace"] = append_trace(
            state, component="replanning", action="create_follow_up", status="succeeded",
            metadata={"replan_count": replan_no, "action_count": 3}
        )
        state["agentic_trace"].extend([
            {"event": "evidence_gap_detected", "status": assessment_status, "gap": gap.model_dump(mode="json")},
            {"event": "replan_created", "replan_count": replan_no, "action_ids": [search_id, scrape_id, verify_id]},
            {"event": "follow_up_tool_selected", "action_id": search_id, "tool": ToolName.REQUEST_MORE_EVIDENCE.value},
        ])

    @staticmethod
    def _latest_verification_action_id(state: AgenticExecutionState) -> str:
        for action_id in reversed(state["completed_action_ids"]):
            result = state["tool_results"].get(action_id)
            if result and result.tool == ToolName.VERIFY_CLAIM.value:
                return action_id
        return state["completed_action_ids"][-1]

    @staticmethod
    def _max_replans(plan: ResearchPlan) -> int:
        return min(3, max(0, int(getattr(plan, "max_replans", 2))))

    def _execute_action(
        self,
        action: PlannedAction,
        state: AgenticExecutionState,
        context: Mapping[str, Any],
    ) -> list[ToolResult]:
        if action.tool == ToolName.SCRAPE_SOURCE:
            sources = self._sources_for_scrape_action(action, state)
            if not sources:
                return [self._invalid_input_result(action, "no sources available from completed search dependencies")]
            results: list[ToolResult] = []
            for source in sources:
                if state["tool_call_count"] >= state["research_plan"].stopping_criteria.max_tool_calls:
                    break
                payload = {"url": str(source.url), "title": source.title, "source_id": source.source_id}
                results.append(self._execute_registered(action, payload, state))
            return results or [self._invalid_input_result(action, "tool/action budget exhausted before scraping")]

        try:
            payload = self._build_payload(action, state, context)
        except Exception as exc:  # noqa: BLE001
            return [self._invalid_input_result(action, str(exc))]
        return [self._execute_registered(action, payload, state)]

    def _execute_registered(
        self, action: PlannedAction, payload: Mapping[str, Any], state: AgenticExecutionState
    ) -> ToolResult:
        started = time.perf_counter()
        result = self.registry.execute(action.tool, dict(payload))
        duration_ms = result.duration_ms or int((time.perf_counter() - started) * 1000)
        state["tool_call_count"] += 1
        state["tool_trace"].append(self._trace(action, result, duration_ms))
        state["research_trace"] = append_trace(
            state,
            component="tool_execution",
            action="execute",
            status="succeeded" if result.success else "failed",
            duration_ms=duration_ms,
            metadata={"tool": str(action.tool), "action_id": action.action_id, "tool_call_count": state["tool_call_count"]},
            error=result.error.message if result.error else None,
        )
        state["research_trace"] = append_trace(
            state, component="tool_result", action="record", status="succeeded" if result.success else "failed",
            metadata={"tool": str(action.tool), "action_id": action.action_id, "has_output": result.success}
        )
        return result

    @staticmethod
    def _invalid_input_result(action: PlannedAction, message: str) -> ToolResult:
        return ToolResult(
            tool=str(action.tool),
            success=False,
            error={"code": "invalid_input", "message": message[:300], "details": []},
            duration_ms=0,
        )

    def to_graph_state(self, state: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Run the validated plan from graph state and return only safe state fields.

        This bridge is intentionally explicit rather than wired into the legacy
        graph topology, preserving the existing pipeline as the fallback.
        """
        plan = state.get("research_plan")
        if not isinstance(plan, ResearchPlan):
            return {
                **state,
                "agentic_status": "failed",
                "agentic_error": "no validated research_plan in graph state",
            }
        result = self.run(
            plan,
            execution_context=kwargs.get("execution_context"),
            initial_state=state,
        )
        return {**state, **result}

    def _build_payload(
        self,
        action: PlannedAction,
        state: AgenticExecutionState,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        tool = ToolName(action.tool)
        plan = state["research_plan"]
        if tool == ToolName.SEARCH_WEB:
            return {
                "query": action.query,
                "max_results": action.max_results or 10,
                "research_mode": plan.research_mode,
            }
        if tool == ToolName.SEARCH_EXISTING_EVIDENCE:
            user_id = context.get("user_id")
            if not isinstance(user_id, str) or not user_id:
                raise ValueError("trusted execution_context.user_id is required for search_existing_evidence")
            return {
                "query": action.query,
                "user_id": user_id,
                "limit": min(action.max_results or 5, 10),
                "topic": plan.question,
            }
        if tool == ToolName.SCRAPE_SOURCE:
            sources = self._sources_for_scrape_action(action, state)
            if not sources:
                raise ValueError("no sources available from completed search dependencies")
            # A scrape action is expanded into one registered scrape_source call
            # per intended source by the executor. The returned payload here is
            # the first typed call; subsequent calls use the same schema.
            source = sources[0]
            return {
                "url": str(source.url),
                "title": source.title,
                "source_id": source.source_id,
            }
        if tool == ToolName.VERIFY_CLAIM:
            evidence = self._evidence_text(state)
            claim = plan.objective
            return {"claim": claim[:1000], "evidence_text": evidence[:200_000]}
        if tool == ToolName.REQUEST_MORE_EVIDENCE:
            gap = self._gap_for_action(action.action_id, state)
            return {
                "question": plan.question,
                "gap_description": gap.missing_evidence if gap else self._gap_description(plan, state),
                "exclude_urls": self._all_source_urls(state),
                "max_results": action.max_results or 5,
                "research_mode": plan.research_mode,
            }
        raise ValueError("unsupported planned tool")

    @staticmethod
    def _gap_for_action(action_id: str, state: AgenticExecutionState) -> EvidenceGap | None:
        for history in reversed(state.get("replan_history", [])):
            if action_id in history.get("actions", []):
                raw = history.get("gap")
                if isinstance(raw, dict):
                    try:
                        return EvidenceGap.model_validate(raw)
                    except Exception:
                        return None
        return None

    @staticmethod
    def _sources_for_scrape_action(action: PlannedAction, state: AgenticExecutionState):
        sources = []
        seen: set[str] = set()
        # Only consume sources produced by this scrape action's declared
        # dependencies. This preserves plan data-flow and prevents scraping
        # unrelated results from another action/user context.
        for dep_id in action.depends_on:
            result = state["tool_results"].get(dep_id)
            if result is None or not result.success:
                continue
            output = result.output
            if isinstance(output, SearchWebOutput):
                candidates = output.sources
            elif isinstance(output, RequestMoreEvidenceOutput):
                candidates = output.new_sources
            else:
                candidates = []
            for source in candidates:
                key = str(source.url)
                if key not in seen:
                    seen.add(key)
                    sources.append(source)
        return sources

    @staticmethod
    def _all_source_urls(state: AgenticExecutionState) -> list[str]:
        urls: list[str] = []
        all_results = [r for results in state.get("action_tool_results", {}).values() for r in results]
        for result in all_results:
            output = result.output
            if isinstance(output, SearchWebOutput):
                urls.extend(str(s.url) for s in output.sources)
            elif isinstance(output, RequestMoreEvidenceOutput):
                urls.extend(str(s.url) for s in output.new_sources)
        return list(dict.fromkeys(urls))[:100]

    @staticmethod
    def _evidence_text(state: AgenticExecutionState) -> str:
        chunks: list[str] = []
        for results in state.get("action_tool_results", {}).values():
            for result in results:
                output = result.output
                if isinstance(output, ScrapeSourceOutput) and output.scraped and output.success:
                    chunks.append(output.scraped.content)
                elif isinstance(output, SearchExistingEvidenceOutput):
                    chunks.extend(hit.summary for hit in output.hits if hit.summary)
        return "\n\n".join(chunks)

    @classmethod
    def _calculate_metrics(cls, plan: ResearchPlan, state: AgenticExecutionState) -> dict[str, Any]:
        sources = []
        domains = set()
        evidence_by_sq: dict[str, int] = {sq.sub_question_id: 0 for sq in plan.sub_questions}
        verified_ratio = 0.0
        verified_seen = False

        all_results = [r for results in state.get("action_tool_results", {}).values() for r in results]
        for result in all_results:
            output = result.output
            if isinstance(output, SearchWebOutput):
                sources.extend(output.sources)
                for s in output.sources:
                    domains.add(getattr(s, "domain", "") or "")
            elif isinstance(output, RequestMoreEvidenceOutput):
                sources.extend(output.new_sources)
                for s in output.new_sources:
                    domains.add(getattr(s, "domain", "") or "")
            elif isinstance(output, SearchExistingEvidenceOutput):
                # Existing evidence can satisfy one source slot per returned hit.
                sources.extend(output.hits)
            elif isinstance(output, ScrapeSourceOutput) and output.success:
                if output.scraped:
                    domains.add(output.scraped.domain)
            elif isinstance(output, VerifyClaimOutput):
                verified_seen = True
                claims = output.verification.claims
                if claims:
                    verified = sum(1 for c in claims if c.status == "verified")
                    verified_ratio = verified / len(claims)

        source_count = len({getattr(s, "url", None) or getattr(s, "report_id", None) for s in sources})
        # A conservative coverage metric: each successful evidence-producing
        # action counts toward the sub-questions it was planned to cover.
        successful_actions = {
            action_id for action_id, result in state["tool_results"].items() if result.success
        }
        for action in plan.planned_actions:
            if action.action_id in successful_actions and ToolName(action.tool) in {
                ToolName.SEARCH_WEB, ToolName.SEARCH_EXISTING_EVIDENCE, ToolName.SCRAPE_SOURCE
            }:
                for sq in action.sub_question_ids:
                    evidence_by_sq[sq] += 1
        coverage = sum(v > 0 for v in evidence_by_sq.values()) / max(1, len(evidence_by_sq))

        failure_count = sum(1 for record in state.get("tool_trace", []) if record.get("status") == "failed")
        retry_count = sum(int(record.get("retry_count", 0) or 0) for record in state.get("tool_trace", []))
        return {
            "source_count": source_count,
            "failure_count": failure_count,
            "retry_count": retry_count,
            "distinct_domain_count": len({d for d in domains if d}),
            "requirement_coverage": coverage,
            "verified_claim_ratio": verified_ratio if verified_seen else 0.0,
        }

    @staticmethod
    def _gap_description(plan: ResearchPlan, state: AgenticExecutionState) -> str:
        metrics = AgenticToolExecutor._calculate_metrics(plan, state)
        gaps = []
        if metrics["source_count"] < plan.stopping_criteria.target_source_count:
            gaps.append("source count below target")
        if metrics["distinct_domain_count"] < plan.stopping_criteria.min_distinct_domains:
            gaps.append("not enough distinct domains")
        if metrics["requirement_coverage"] < plan.stopping_criteria.min_requirement_coverage:
            gaps.append("evidence requirement coverage below threshold")
        if metrics["verified_claim_ratio"] < plan.stopping_criteria.min_verified_claim_ratio:
            gaps.append("verified claim ratio below threshold")
        return "; ".join(gaps) or "No remaining evidence gap identified."

    @staticmethod
    def _trace(action: PlannedAction, result: ToolResult, duration_ms: int) -> ActionExecutionRecord:
        metadata: dict[str, Any] = {}
        if result.success and result.output is not None:
            output = result.output
            if hasattr(output, "model_dump"):
                dumped = output.model_dump(mode="json")
                # Trace only safe aggregate metadata, never full tool inputs or
                # sensitive evidence payloads.
                if isinstance(dumped, dict):
                    for key in ("raw_result_count", "filtered_already_seen", "success"):
                        if key in dumped:
                            metadata[key] = dumped[key]
                    if "sources" in dumped:
                        metadata["source_count"] = len(dumped["sources"])
                    if "new_sources" in dumped:
                        metadata["new_source_count"] = len(dumped["new_sources"])
                    if "hits" in dumped:
                        metadata["hit_count"] = len(dumped["hits"])
        return {
            "action_id": action.action_id,
            "tool": str(action.tool),
            "status": "succeeded" if result.success else "failed",
            "duration_ms": duration_ms,
            "result_metadata": metadata,
            "error": (
                {
                    "code": str(result.error.code),
                    "message": (
                        "tool input validation failed"
                        if str(result.error.code) == "invalid_input"
                        else "tool execution failed"
                    ),
                    "details": list(result.error.details),
                    "error_type": classify_error(result.error.message if result.error else None),
                    "retry_count": 0,
                }
                if result.error
                else None
            ),
        }

    @staticmethod
    def _record_failure(state: AgenticExecutionState, action: PlannedAction, code: str, message: str) -> None:
        state["tool_trace"].append({
            "action_id": action.action_id,
            "tool": str(action.tool),
            "status": "failed",
            "duration_ms": 0,
            "result_metadata": {},
            "error": {"code": code, "message": message, "error_type": classify_error(message), "retry_count": 0},
        })

    @staticmethod
    def _failed_state(message: str) -> AgenticExecutionState:
        return {
            "research_plan": None,  # type: ignore[typeddict-item]
            "current_action_id": None,
            "completed_action_ids": [],
            "tool_results": {},
            "action_tool_results": {},
            "tool_trace": [],
            "tool_call_count": 0,
            "agentic_status": "failed",
            "agentic_error": message,
            "execution_metrics": {},
            "verification_status": "failed",
            "evidence_gaps": [],
            "replan_count": 0,
            "replan_history": [],
            "newly_discovered_sources": [],
            "agentic_trace": [],
            "research_trace": [],
        }
