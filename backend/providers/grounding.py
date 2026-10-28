"""Grounding analysis: do the citations in an answer point at real evidence,
and does that evidence lexically contain what the sentence says?

IMPORTANT LIMITS (also shown to users): this is a lexical-overlap heuristic.
It can confirm that a cited passage *contains the same words and numbers*, not
that the claim is true or fully supported, and it can miss paraphrases. It is
useful for catching fabricated labels, uncited prose and numbers that appear
nowhere in the evidence. It is not an accuracy score.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .jsonschema_lite import validate
from .prompts import ANSWER_SCHEMA

METHOD = "lexical-overlap heuristic (not a semantic or factual judgement)"
_LABEL_GROUP = re.compile(r"\[\s*((?:[DW]\d{1,3})(?:\s*[,;]\s*[DW]\d{1,3})*)\s*\]")
_WORD = re.compile(r"[a-z][a-z'\-]{2,}")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?%?")
_STOP = set("""the and for are was were has have had that this with from not but its their they them his her she him you your our
who whom what when where which while will would could should can may might shall into onto over under than then also been being
more most some such only own same very just about after before between during each other any all both few many much per via
there here these those how why does did doing done said says say use used using one two""".split())
_ABSTAIN = re.compile(r"(?i)(insufficient|not (?:enough|sufficient)|does not (?:contain|say|mention|provide|specify|state)|"
                      r"do(?:es)? not (?:include|cover)|(?:no|not) (?:information|evidence|mention|data) (?:about|on|regarding|in|is)|"
                      r"cannot (?:be determined|determine|answer|find)|can't (?:determine|answer|find)|unable to (?:determine|answer|find)|"
                      r"not (?:provided|stated|specified|available|addressed)|i (?:do not|don't) (?:know|have))")


def extract_labels(text: str) -> list[str]:
    out: list[str] = []
    for m in _LABEL_GROUP.finditer(text or ""):
        out += [x.strip() for x in re.split(r"[,;]", m.group(1))]
    return out


def strip_labels(text: str) -> str:
    return _LABEL_GROUP.sub("", text or "")


def split_sentences(text: str) -> list[str]:
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    t = re.sub(r"^\s*[#>*\-\d.\)]+\s*", "", t, flags=re.M)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\[\"'(])|\n{1,}", t)
    return [p.strip() for p in parts if p and p.strip()]


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def content_words(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(text or "")}


def lexical_support(sentence: str, evidence_texts: list[str]) -> tuple[bool, float]:
    """(supported, overlap). Needs >=50% content-word overlap AND every number present."""
    s = strip_labels(sentence)
    words = content_words(s) | numbers(s)
    if len(words) < 2:
        return False, 0.0
    ev = " ".join(evidence_texts)
    ev_words = content_words(ev) | numbers(ev)
    overlap = len(words & ev_words) / len(words)
    nums = numbers(s)
    nums_ok = nums <= numbers(ev)
    return (overlap >= 0.5 and nums_ok), round(overlap, 3)


def detect_abstain(text: str) -> bool:
    return bool(_ABSTAIN.search(text or ""))


def parse_structured(text: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Extract and validate the {answer, claims, abstained} object. Tolerates code fences / prose around it."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return None, ["no JSON object found"]
    try:
        data = json.loads(t[start:end + 1])
    except ValueError:
        return None, ["invalid JSON"]
    errs = validate(data, ANSWER_SCHEMA)
    return (data if not errs else None), errs


def analyze_atlas(answer: str, sources: list[dict[str, Any]], claims: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    by_id = {s["id"]: s for s in sources}
    mentions = extract_labels(answer)
    valid_m = [m for m in mentions if m in by_id]
    invalid = sorted({m for m in mentions if m not in by_id})
    notes: list[str] = []
    checked = supported = 0
    covered = factual = 0
    if claims is not None:
        for c in claims:
            labels = [x for x in c.get("sources", []) if x in by_id]
            bad = [x for x in c.get("sources", []) if x not in by_id]
            invalid = sorted(set(invalid) | set(bad))
            factual += 1
            if labels:
                covered += 1
                checked += 1
                ok, _ = lexical_support(c.get("text", ""), [by_id[x]["excerpt"] for x in labels])
                supported += int(ok)
        notes.append("Claims came from the model's structured output; each cited claim was checked against its cited passages.")
    else:
        for sent in split_sentences(answer):
            if sent.endswith("?") or len(strip_labels(sent).split()) < 4 or detect_abstain(sent):
                continue
            factual += 1
            labs = [m for m in extract_labels(sent) if m in by_id]
            if labs:
                covered += 1
                checked += 1
                ok, _ = lexical_support(sent, [by_id[x]["excerpt"] for x in set(labs)])
                supported += int(ok)
    if not sources:
        notes.append("No evidence passages were supplied, so citations cannot be checked.")
    if factual == 0:
        notes.append("No checkable factual sentences were found (the answer may be an abstention or very short).")
    return {
        "mode": "atlas",
        "cited_labels": sorted(set(valid_m)),
        "invalid_labels": invalid,
        "valid_citation_rate": round(len(valid_m) / len(mentions), 3) if mentions else None,
        "sentence_coverage": round(covered / factual, 3) if factual else None,
        "support_rate": round(supported / checked, 3) if checked else None,
        "supported": supported, "checked": checked,
        "method": METHOD, "notes": notes,
    }


def analyze_provider(citations: list[dict[str, Any]], search_calls: int) -> dict[str, Any]:
    valid = [c for c in citations if c.get("valid")]
    notes = ["Sources were found by the provider's own search tool. Atlas did not fetch or verify these pages, "
             "and different providers search different sources, so results are not directly comparable."]
    if not citations:
        notes.append("The provider returned no source links" + (" even though it searched." if search_calls else "."))
    return {"mode": "provider_search", "cited_labels": [], "invalid_labels": [],
            "valid_citation_rate": round(len(valid) / len(citations), 3) if citations else None,
            "sentence_coverage": None, "support_rate": None, "supported": 0, "checked": 0,
            "method": "provider-reported links; URL format check only", "notes": notes}
