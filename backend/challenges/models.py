"""Vocabulary and limits for M7 challenges."""
from __future__ import annotations

CHALLENGE_TYPES = (
    "counter_evidence",
    "contradiction_search",
    "source_quality",
    "temporal_validity",
    "assumption_check",
    "alternative_explanation",
)
STATUSES = ("pending", "running", "completed", "failed")
RESULTS = ("supported", "weakened", "contradicted", "unresolved", "stale", "source_concern")

MAX_ATTEMPTS = 3                 # a failed challenge may be retried this many times in total
RUNNING_LEASE_SECONDS = 900      # a 'running' challenge older than this is considered abandoned
MIN_CLAIM_CHARS = 10             # verify_claim's own minimum
MAX_QUERIES = 2                  # challenge queries per run
RESULTS_PER_QUERY = 5
MAX_SCRAPES = 6                  # pages fetched per run
EVIDENCE_CHARS_PER_SOURCE = 6000  # bounds what is handed to verification
