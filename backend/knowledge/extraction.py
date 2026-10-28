"""Entity extraction.

``HeuristicEntityExtractor`` is deterministic, offline and *conservative*: it
prefers missing an entity to inventing one. It finds acronyms, runs of two or
more capitalised words, and mid-sentence capitalised single words. It cannot
type entities reliably, so the type is only ``acronym`` / ``proper_noun``. An
LLM- or NER-backed extractor can replace it through the ``EntityExtractor``
protocol without any schema change.
"""
from __future__ import annotations

import re
from typing import Protocol

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9&'\-]*")
_CONNECTORS = {"of", "for", "and", "de", "von", "van", "the"}
_LEADING = {
    "the", "a", "an", "this", "that", "these", "those", "however", "but", "and", "in", "on", "at",
    "for", "of", "it", "its", "he", "she", "they", "we", "our", "their", "according", "while",
    "when", "as", "with", "by", "from", "to", "if", "also", "overall", "some", "many", "most",
}
_ACRONYM_STOP = {"AND", "THE", "FOR", "NOT", "BUT", "ARE", "WAS", "ALL", "ANY", "CAN", "OR", "IT", "IS", "AS", "AT", "IN", "ON", "OF", "TO", "BY", "AN", "A", "NO", "SO", "IF", "WE", "BE"}
# Calendar words are usually temporal context, not named entities. Keep them
# out of the entity graph unless a multi-token proper name supplies context.
_MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"}


class EntityExtractor(Protocol):
    def extract(self, text: str) -> list[tuple[str, str]]:
        """Return ``(surface_name, entity_type)`` pairs, de-duplicated."""


class HeuristicEntityExtractor:
    def __init__(self, max_entities: int = 8) -> None:
        self.max_entities = max_entities

    def extract(self, text: str) -> list[tuple[str, str]]:
        found: dict[str, tuple[str, str]] = {}
        words = list(_WORD.finditer(text or ""))

        def sentence_initial(m: re.Match) -> bool:
            before = text[: m.start()].rstrip()
            return not before or before[-1] in ".!?:;\n"

        def cap(w: str) -> bool:
            return w[0].isupper() and not (len(w) > 1 and w.isupper())

        i = 0
        while i < len(words):
            m = words[i]
            w = m.group(0)
            if len(w) > 1 and w.isupper() and 2 <= len(w) <= 6 and w not in _ACRONYM_STOP:
                found.setdefault(w.lower(), (w, "acronym"))
                i += 1
                continue
            if not cap(w):
                i += 1
                continue
            run = [i]
            j = i + 1
            while j < len(words):
                nxt = words[j].group(0)
                gap = text[words[j - 1].end() : words[j].start()]
                if gap.strip() or "\n" in gap:
                    break
                if cap(nxt):
                    run.append(j)
                    j += 1
                elif (nxt.lower() in _CONNECTORS and j + 1 < len(words) and cap(words[j + 1].group(0))
                      and not text[words[j].end() : words[j + 1].start()].strip()):
                    run.extend([j, j + 1])
                    j += 2
                else:
                    break
            toks = [words[k].group(0) for k in run]
            initial = sentence_initial(words[run[0]])
            stripped = False
            while toks and toks[0].lower() in _LEADING:
                toks.pop(0)
                stripped = True
            if len(toks) >= 2 or (len(toks) == 1 and (stripped or not initial) and toks[0].lower() not in _LEADING):
                name = " ".join(toks)
                if len(toks) == 1 and toks[0].lower() in _MONTHS:
                    i = run[-1] + 1
                    continue
                if len(name) > 2:
                    found.setdefault(name.lower(), (name, "proper_noun"))
            i = run[-1] + 1
        return list(found.values())[: self.max_entities]
