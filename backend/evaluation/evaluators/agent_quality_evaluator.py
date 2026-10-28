from __future__ import annotations

from collections import Counter
from backend.agentic.common import ToolName
from backend.evaluation.models import AgentQualityEvaluation, EvalFlag


def evaluate_agent_quality(*, research_plan, tool_trace, replan_count, replan_history,
                           agentic_trace, completed_action_ids, tool_call_count, agentic_status,
                           registered_tools=None) -> AgentQualityEvaluation:
    planned = {a.action_id: a for a in (getattr(research_plan, "planned_actions", None) or [])} if research_plan else {}
    required_ids = set(planned)
    completed = set(completed_action_ids or [])
    successful_by_action = {r.get("action_id") for r in (tool_trace or []) if r.get("status") == "succeeded" and r.get("action_id")}

    # Re-planning creates additional mandatory work. Treat every action in a
    # replan history entry as required adaptive work, but evaluate completion
    # at action level so legitimate per-source scrape fan-out (multiple tool
    # trace rows for one scrape action) is not counted as missing/duplicate
    # plan work. Only successful tool traces satisfy an action; an action merely
    # appearing in completed_action_ids is not enough because failed attempts
    # are retained there to prevent retry loops.
    replanned_ids = {
        action_id
        for history in (replan_history or [])
        for action_id in (history.get("actions", []) if isinstance(history, dict) else [])
        if action_id
    }
    all_required_ids = required_ids | replanned_ids
    initial_plan_completion = (
        len(required_ids & successful_by_action) / len(required_ids)
        if required_ids else None
    )
    adaptive_recovery_completion = (
        len(replanned_ids & successful_by_action) / len(replanned_ids)
        if replanned_ids else None
    )
    plan_completion = (
        len(all_required_ids & successful_by_action) / len(all_required_ids)
        if all_required_ids else None
    )

    allowed = set(registered_tools or [t.value for t in ToolName])
    valid = 0
    for record in tool_trace or []:
        tool = str(record.get("tool", ""))
        if tool in allowed:
            valid += 1
    selection_validity = (valid / len(tool_trace)) if tool_trace else 1.0

    # Repeated research actions are measured by tool + normalized purpose. A
    # multi-source scrape action is intentionally not penalized for its per-
    # source tool calls because one planned scrape can legitimately fan out.
    fingerprints = []
    action_id_counts = Counter()
    for record in tool_trace or []:
        tool = str(record.get("tool", ""))
        action_id = str(record.get("action_id", ""))
        if tool != ToolName.SCRAPE_SOURCE.value:
            action_id_counts[action_id] += 1
        if tool in {ToolName.SEARCH_WEB.value, ToolName.REQUEST_MORE_EVIDENCE.value, ToolName.SEARCH_EXISTING_EVIDENCE.value}:
            action_id = str(record.get("action_id", ""))
            action = planned.get(action_id)
            purpose = " ".join(str(getattr(action, "purpose", "" )).lower().split()) if action else action_id
            fingerprints.append((tool, purpose))
    counts = Counter(fingerprints)
    duplicate_actions = max(sum(max(0, n - 1) for n in counts.values()), sum(max(0, n - 1) for n in action_id_counts.values()))
    research_calls = len(fingerprints)
    efficiency = 1.0 if research_calls == 0 else max(0.0, 1.0 - duplicate_actions / research_calls)

    events = list(agentic_trace or [])
    replans = int(replan_count or 0)
    recovery_successes = 0
    for idx, event in enumerate(events):
        if event.get("event") != "replan_created" or event.get("status") == "max_replans_reached":
            continue
        for later in events[idx + 1:]:
            if later.get("event") == "verification_result":
                if later.get("status") == "supported":
                    recovery_successes += 1
                break
    replanning_effectiveness = (recovery_successes / replans) if replans else 1.0
    max_calls = getattr(getattr(research_plan, "stopping_criteria", None), "max_tool_calls", tool_call_count)
    verification_supported = any(e.get("event") == "verification_result" and e.get("status") == "supported" for e in events)
    required_done = required_ids.issubset(successful_by_action) if required_ids else True
    stopping_appropriate = 1.0 if tool_call_count <= max_calls and ((agentic_status == "completed" and (verification_supported or required_done)) or (agentic_status == "completed_with_gaps" and (replans and replan_count >= 1))) else 0.0
    duplicate_rate = duplicate_actions / research_calls if research_calls else 0.0
    flags = []
    if duplicate_actions:
        flags.append(EvalFlag(code="redundant_research_actions", severity="warning", message=f"{duplicate_actions} redundant research action(s) were detected."))
    if replans and recovery_successes == 0:
        flags.append(EvalFlag(code="ineffective_replanning", severity="warning", message="Re-planning occurred but no subsequent verification reached supported status."))

    return AgentQualityEvaluation(
        plan_completion=round(plan_completion, 4) if plan_completion is not None else None,
        initial_plan_completion=round(initial_plan_completion, 4) if initial_plan_completion is not None else None,
        adaptive_recovery_completion=round(adaptive_recovery_completion, 4) if adaptive_recovery_completion is not None else None,
        tool_selection_validity=round(selection_validity, 4),
        unnecessary_tool_calls=duplicate_actions,
        tool_efficiency=round(efficiency, 4),
        replan_count=replans,
        replan_effectiveness=round(replanning_effectiveness, 4),
        successful_recoveries=recovery_successes,
        appropriate_stopping=stopping_appropriate,
        duplicate_research_actions=duplicate_actions,
        duplicate_research_rate=round(duplicate_rate, 4),
        score=None,
        flags=flags,
    ).with_composite()
