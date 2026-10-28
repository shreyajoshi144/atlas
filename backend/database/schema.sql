PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS topics (
    topic_id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users (user_id),
    topic TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, topic)
);

CREATE TABLE IF NOT EXISTS research_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users (user_id),
    topic_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    status TEXT NOT NULL,
    report_id TEXT,
    query_text TEXT,
    error_message TEXT,
    retrieval_latency_ms INTEGER NOT NULL DEFAULT 0,
    scraping_latency_ms INTEGER NOT NULL DEFAULT 0,
    llm_latency_ms INTEGER NOT NULL DEFAULT 0,
    total_execution_ms INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (topic_id) REFERENCES topics (topic_id)
);

CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    summary TEXT NOT NULL,
    report_markdown TEXT NOT NULL,
    key_statistics_json TEXT NOT NULL,
    swot_json TEXT NOT NULL,
    verification_score REAL,
    verification_verdict TEXT,
    verification_json TEXT,
    executive_brief_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES research_sessions (session_id),
    FOREIGN KEY (topic_id) REFERENCES topics (topic_id)
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    snippet TEXT NOT NULL DEFAULT '',
    publish_date TEXT,
    bm25_score REAL NOT NULL DEFAULT 0.0,
    tfidf_score REAL NOT NULL DEFAULT 0.0,
    cosine_score REAL NOT NULL DEFAULT 0.0,
    fuzz_score REAL NOT NULL DEFAULT 0.0,
    relevance_score REAL NOT NULL DEFAULT 0.0,
    credibility_score REAL NOT NULL DEFAULT 0.0,
    rank_position INTEGER NOT NULL DEFAULT 0,
    source_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (report_id) REFERENCES reports (report_id),
    FOREIGN KEY (session_id) REFERENCES research_sessions (session_id)
);

CREATE TABLE IF NOT EXISTS report_chunks (
    chunk_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (report_id) REFERENCES reports (report_id),
    FOREIGN KEY (session_id) REFERENCES research_sessions (session_id),
    FOREIGN KEY (topic_id) REFERENCES topics (topic_id)
);

CREATE TABLE IF NOT EXISTS analytics_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES research_sessions (session_id)
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Persistent student notes: one note per (user, case). Deliberately simple —
-- no folders/tags — matching the single-textarea UX it replaces.
CREATE TABLE IF NOT EXISTS notes (
    note_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id),
    FOREIGN KEY (report_id) REFERENCES reports (report_id),
    UNIQUE (user_id, report_id)
);

-- Password reset tokens: only the SHA-256 hash of the raw token is ever
-- stored (see hash_reset_token) — the raw token exists only in memory and
-- in the one-time link. Single-use (used_at) and time-limited (expires_at).
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);

-- Non-blocking pipeline evaluations, one row per research run that
-- completed evaluation (see backend/evaluation/). Never a hard dependency
-- for reports/sources — a research run that fails evaluation entirely
-- simply has no row here (see EvaluationRepository.save_evaluation and
-- its non-fatal caller in api/routes/research.py).
CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    topic_id TEXT,
    overall_score REAL,
    retrieval_score REAL,
    grounding_score REAL,
    verification_quality_score REAL,
    answer_groundedness_score REAL,
    research_quality_score REAL,
    agent_quality_score REAL,
    total_latency_ms INTEGER,
    tool_call_count INTEGER,
    replan_count INTEGER,
    total_tokens INTEGER,
    estimated_cost_usd REAL,
    flags_json TEXT NOT NULL DEFAULT '[]',
    details_json TEXT NOT NULL,
    errors_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY (report_id) REFERENCES reports (report_id),
    FOREIGN KEY (session_id) REFERENCES research_sessions (session_id)
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);
CREATE INDEX IF NOT EXISTS idx_reset_tokens_user_id ON password_reset_tokens (user_id);
CREATE INDEX IF NOT EXISTS idx_reset_tokens_token_hash ON password_reset_tokens (token_hash);
CREATE INDEX IF NOT EXISTS idx_topics_topic ON topics (topic);
CREATE INDEX IF NOT EXISTS idx_topics_user_id ON topics (user_id);
CREATE INDEX IF NOT EXISTS idx_reports_topic_id ON reports (topic_id);
CREATE INDEX IF NOT EXISTS idx_reports_session_id ON reports (session_id);
CREATE INDEX IF NOT EXISTS idx_sources_report_id ON sources (report_id);
CREATE INDEX IF NOT EXISTS idx_sources_session_id ON sources (session_id);
CREATE INDEX IF NOT EXISTS idx_sources_domain ON sources (domain);
CREATE INDEX IF NOT EXISTS idx_sessions_topic_id ON research_sessions (topic_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON research_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON research_sessions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chunks_report_id ON report_chunks (report_id);
CREATE INDEX IF NOT EXISTS idx_chunks_session_id ON report_chunks (session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_topic ON report_chunks (topic);
CREATE INDEX IF NOT EXISTS idx_events_session_id ON analytics_events (session_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON analytics_events (event_type);
CREATE INDEX IF NOT EXISTS idx_notes_user_report ON notes (user_id, report_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_report_id ON evaluations (report_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_session_id ON evaluations (session_id);
-- ── M5 knowledge graph: Source / Evidence / Claim / Entity + relationships ──
-- Object tables are user-namespaced and content-addressed (see backend/knowledge/ids.py).
-- Link tables carry report_id so one report's contribution can be replaced or
-- deleted without touching other investigations; orphaned objects are GC'd.
CREATE TABLE IF NOT EXISTS knowledge_sources (
    source_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    publish_date TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (user_id, canonical_url),
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
CREATE TABLE IF NOT EXISTS knowledge_evidence (
    evidence_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    passage_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id),
    FOREIGN KEY (source_id) REFERENCES knowledge_sources (source_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS knowledge_claims (
    claim_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
CREATE TABLE IF NOT EXISTS knowledge_entities (
    entity_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'proper_noun',
    created_at TEXT NOT NULL,
    UNIQUE (user_id, canonical_name),
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
CREATE TABLE IF NOT EXISTS knowledge_report_sources (
    report_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    report_source_id TEXT NOT NULL DEFAULT '',
    rank_position INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT,
    PRIMARY KEY (report_id, source_id),
    FOREIGN KEY (report_id) REFERENCES reports (report_id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES knowledge_sources (source_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS knowledge_claim_observations (
    report_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (report_id, claim_id),
    FOREIGN KEY (report_id) REFERENCES reports (report_id) ON DELETE CASCADE,
    FOREIGN KEY (claim_id) REFERENCES knowledge_claims (claim_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS knowledge_claim_evidence (
    report_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    stance TEXT NOT NULL DEFAULT 'mixed',
    strength REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (report_id, claim_id, evidence_id),
    FOREIGN KEY (report_id) REFERENCES reports (report_id) ON DELETE CASCADE,
    FOREIGN KEY (claim_id) REFERENCES knowledge_claims (claim_id) ON DELETE CASCADE,
    FOREIGN KEY (evidence_id) REFERENCES knowledge_evidence (evidence_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS knowledge_claim_entities (
    report_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY (report_id, claim_id, entity_id),
    FOREIGN KEY (report_id) REFERENCES reports (report_id) ON DELETE CASCADE,
    FOREIGN KEY (claim_id) REFERENCES knowledge_claims (claim_id) ON DELETE CASCADE,
    FOREIGN KEY (entity_id) REFERENCES knowledge_entities (entity_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS knowledge_evidence_entities (
    report_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY (report_id, evidence_id, entity_id),
    FOREIGN KEY (report_id) REFERENCES reports (report_id) ON DELETE CASCADE,
    FOREIGN KEY (evidence_id) REFERENCES knowledge_evidence (evidence_id) ON DELETE CASCADE,
    FOREIGN KEY (entity_id) REFERENCES knowledge_entities (entity_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_kn_sources_user ON knowledge_sources (user_id);
CREATE INDEX IF NOT EXISTS idx_kn_claims_user ON knowledge_claims (user_id);
CREATE INDEX IF NOT EXISTS idx_kn_entities_user ON knowledge_entities (user_id);
CREATE INDEX IF NOT EXISTS idx_kn_obs_claim ON knowledge_claim_observations (claim_id);
CREATE INDEX IF NOT EXISTS idx_kn_ce_evidence ON knowledge_claim_evidence (evidence_id);
CREATE INDEX IF NOT EXISTS idx_kn_cent_entity ON knowledge_claim_entities (entity_id);
CREATE INDEX IF NOT EXISTS idx_kn_eent_entity ON knowledge_evidence_entities (entity_id);

-- M5 closure: explicit state of a report's knowledge graph. 'complete' and
-- 'partial' are written in the same transaction as the graph; 'failed' is
-- written separately after a rollback. No row = never recorded.
CREATE TABLE IF NOT EXISTS knowledge_investigation_status (
    report_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'failed')),
    dropped_sources INTEGER NOT NULL DEFAULT 0,
    dropped_evidence INTEGER NOT NULL DEFAULT 0,
    dropped_claims INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (report_id) REFERENCES reports (report_id) ON DELETE CASCADE
);

-- ── M7 challenges: adversarial investigation of a claim ──────────────────────
-- A challenge is its own persisted event; it NEVER modifies M5 observations or
-- evidence (history is preserved). claim_id is a soft reference on purpose: the
-- challenge keeps claim_text + a base snapshot so it stays meaningful even if the
-- investigation's graph is later rebuilt.
CREATE TABLE IF NOT EXISTS challenges (
    challenge_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    investigation_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    challenge_type TEXT NOT NULL CHECK (challenge_type IN
        ('counter_evidence','contradiction_search','source_quality','temporal_validity','assumption_check','alternative_explanation')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed')),
    result TEXT CHECK (result IS NULL OR result IN
        ('supported','weakened','contradicted','unresolved','stale','source_concern')),
    claim_text TEXT NOT NULL,
    base_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    error_type TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    -- a result exists if and only if the challenge completed
    CHECK ((status = 'completed') = (result IS NOT NULL)),
    FOREIGN KEY (user_id) REFERENCES users (user_id),
    FOREIGN KEY (investigation_id) REFERENCES reports (report_id) ON DELETE CASCADE
);
-- at most ONE active challenge per (user, investigation, claim, type): create is idempotent
CREATE UNIQUE INDEX IF NOT EXISTS uq_challenge_active
    ON challenges (user_id, investigation_id, claim_id, challenge_type) WHERE status IN ('pending','running');
CREATE INDEX IF NOT EXISTS idx_challenges_user_claim ON challenges (user_id, claim_id, created_at);
CREATE INDEX IF NOT EXISTS idx_challenges_user_inv ON challenges (user_id, investigation_id);

CREATE TABLE IF NOT EXISTS challenge_attempts (
    challenge_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_type TEXT,
    trace_json TEXT,
    PRIMARY KEY (challenge_id, attempt_no),
    FOREIGN KEY (challenge_id) REFERENCES challenges (challenge_id) ON DELETE CASCADE
);

-- every source actually retrieved (and whether it was scraped) during a challenge
CREATE TABLE IF NOT EXISTS challenge_sources (
    challenge_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    domain TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    publish_date TEXT,
    content_hash TEXT,
    scraped INTEGER NOT NULL DEFAULT 0,
    independent INTEGER NOT NULL DEFAULT 1,
    query_index INTEGER NOT NULL DEFAULT 0,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (challenge_id, source_id),
    FOREIGN KEY (challenge_id) REFERENCES challenges (challenge_id) ON DELETE CASCADE
);

-- evidence MUST come from a retrieved source: enforced by the composite foreign key
CREATE TABLE IF NOT EXISTS challenge_evidence (
    challenge_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    passage_text TEXT NOT NULL,
    stance TEXT NOT NULL CHECK (stance IN ('support','contradict','mixed')),
    strength REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (challenge_id, evidence_id),
    FOREIGN KEY (challenge_id) REFERENCES challenges (challenge_id) ON DELETE CASCADE,
    FOREIGN KEY (challenge_id, source_id) REFERENCES challenge_sources (challenge_id, source_id) ON DELETE CASCADE
);


-- ── M8 AtlasBench: reproducible evaluation of research agents/models ─────────
-- Built-in suites ship with the code and are NOT stored here; only a user's own
-- tasks/configs are. A run keeps full SNAPSHOTS of the config and of every task it
-- measured (with content hashes), so deleting or changing a task never rewrites history
-- and a run can always say exactly what it measured.
CREATE TABLE IF NOT EXISTS bench_tasks (
    task_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    task_hash TEXT NOT NULL,
    gold_quality TEXT NOT NULL CHECK (gold_quality IN ('curated','derived')),
    source_kind TEXT NOT NULL DEFAULT 'custom' CHECK (source_kind IN ('custom','knowledge_claim')),
    task_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
CREATE INDEX IF NOT EXISTS idx_bench_tasks_user ON bench_tasks (user_id, created_at);

CREATE TABLE IF NOT EXISTS bench_configs (
    config_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, name),
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);

CREATE TABLE IF NOT EXISTS bench_runs (
    run_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed')),
    config_id TEXT NOT NULL,                  -- soft reference: presets are not rows
    config_name TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    suite_id TEXT,
    suite_hash TEXT,
    fingerprint TEXT NOT NULL,                -- hash(evaluator version, config hash, task hashes)
    evaluator_version TEXT NOT NULL,
    reproduces_run_id TEXT,
    case_count INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT,
    error_type TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    CHECK ((status = 'completed') = (summary_json IS NOT NULL)),
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
CREATE INDEX IF NOT EXISTS idx_bench_runs_user ON bench_runs (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bench_runs_fp ON bench_runs (user_id, fingerprint);

CREATE TABLE IF NOT EXISTS bench_run_cases (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','completed','failed')),
    task_hash TEXT NOT NULL,
    task_title TEXT NOT NULL,
    task_json TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    composite REAL,
    scores_json TEXT,
    detail_json TEXT,
    output_json TEXT,
    trace_json TEXT,
    raw_llm_json TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    cost_usd REAL,
    latency_ms INTEGER,
    llm_latency_ms INTEGER,
    error_type TEXT,
    started_at TEXT,
    finished_at TEXT,
    PRIMARY KEY (run_id, task_id),
    FOREIGN KEY (run_id) REFERENCES bench_runs (run_id) ON DELETE CASCADE
);

-- M10: append-only arbitration records refer to completed benchmark cases;
-- snapshots include candidate outputs and their provenance for later audit.
CREATE TABLE IF NOT EXISTS bench_arbitrations (
    arbitration_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    task_hash TEXT NOT NULL,
    candidate_run_ids_json TEXT NOT NULL,
    selected_run_id TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
CREATE INDEX IF NOT EXISTS idx_bench_arbitrations_user ON bench_arbitrations (user_id, created_at);


-- ── M9 Cost Autopilot + Model Routing ────────────────────────────────────────
-- A user's routing policies, which one is active (none => the application default, no fallback),
-- and one row per model call made on their behalf. Usage rows hold NO prompts or responses: only
-- which model was asked for, which one answered, why, what it cost and how long it took.
CREATE TABLE IF NOT EXISTS routing_policies (
    policy_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, name),
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);

CREATE TABLE IF NOT EXISTS routing_active (
    user_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id),
    FOREIGN KEY (policy_id) REFERENCES routing_policies (policy_id)
);

CREATE TABLE IF NOT EXISTS llm_usage (
    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    run_ref TEXT NOT NULL,                     -- groups the calls of one request / research run
    purpose TEXT NOT NULL,
    policy_id TEXT,                            -- soft reference: the policy may be deleted later
    policy_name TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('explicit','autopilot')),
    reason_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    actual_model TEXT,                         -- NULL when every attempt failed
    status TEXT NOT NULL CHECK (status IN ('ok','failed')),
    fallback_used INTEGER NOT NULL DEFAULT 0 CHECK (fallback_used IN (0,1)),
    fallback_reason TEXT,
    attempts_json TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    tokens_reported INTEGER NOT NULL DEFAULT 0 CHECK (tokens_reported IN (0,1)),
    est_cost_usd REAL,                         -- estimated before the call, for the requested model
    actual_cost_usd REAL,                      -- legacy name: usage-derived catalog-price estimate, NOT provider-billed cost
    latency_ms INTEGER NOT NULL,               -- wall time including any failed attempts
    llm_latency_ms INTEGER,                    -- the answering attempt only
    error_type TEXT,
    evidence_run_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_user ON llm_usage (user_id, usage_id);

-- M11: rolling operational budgets and immutable admin audit trail.
CREATE TABLE IF NOT EXISTS user_usage_limits (
    user_id TEXT PRIMARY KEY,
    daily_call_limit INTEGER,
    monthly_spend_cap_usd REAL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id),
    FOREIGN KEY (updated_by) REFERENCES users (user_id),
    CHECK (daily_call_limit IS NULL OR daily_call_limit >= 0),
    CHECK (monthly_spend_cap_usd IS NULL OR monthly_spend_cap_usd >= 0)
);
CREATE TABLE IF NOT EXISTS llm_budget_events (
    event_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    actual_model TEXT,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    reserved_cost_usd REAL NOT NULL DEFAULT 0,
    cost_known INTEGER NOT NULL DEFAULT 1 CHECK (cost_known IN (0,1)),
    actual_cost_usd REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    result_status TEXT CHECK (result_status IN ('ok','failed')),
    error_type TEXT,
    latency_ms INTEGER,
    status TEXT NOT NULL CHECK (status IN ('reserved','completed')),
    created_at TEXT NOT NULL,
    expires_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users (user_id)
);
CREATE INDEX IF NOT EXISTS idx_llm_budget_user_time ON llm_budget_events (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_budget_reservation ON llm_budget_events (status, expires_at);
CREATE TABLE IF NOT EXISTS admin_audit_logs (
    audit_id TEXT PRIMARY KEY,
    actor_user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_user_id TEXT,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (actor_user_id) REFERENCES users (user_id),
    FOREIGN KEY (target_user_id) REFERENCES users (user_id)
);
CREATE TABLE IF NOT EXISTS playground_conversations (
    conversation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    messages_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_playground_conversation_user_time
ON playground_conversations (user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_logs (created_at DESC);
CREATE TRIGGER IF NOT EXISTS trg_admin_audit_no_update
BEFORE UPDATE ON admin_audit_logs BEGIN SELECT RAISE(ABORT, 'audit logs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_admin_audit_no_delete
BEFORE DELETE ON admin_audit_logs BEGIN SELECT RAISE(ABORT, 'audit logs are immutable'); END;


-- Workflow Studio: user-owned definitions and persisted deterministic executions.
CREATE TABLE IF NOT EXISTS workflow_definitions (
  workflow_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
  definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_workflow_def_user ON workflow_definitions(user_id, updated_at);
CREATE TABLE IF NOT EXISTS workflow_runs (
  run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
  input_json TEXT NOT NULL, output_json TEXT, error TEXT, created_at TEXT NOT NULL, finished_at TEXT,
  definition_snapshot_json TEXT, snapshot_version INTEGER NOT NULL DEFAULT 1,
  UNIQUE(user_id, workflow_id, idempotency_key),
  FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
  FOREIGN KEY(workflow_id) REFERENCES workflow_definitions(workflow_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_user ON workflow_runs(user_id, created_at);
CREATE TABLE IF NOT EXISTS workflow_run_steps (
  run_id TEXT NOT NULL, step_index INTEGER NOT NULL, step_name TEXT NOT NULL,
  step_type TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('completed','failed','skipped')),
  input_json TEXT NOT NULL, output_json TEXT, error TEXT, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
  config_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(run_id, step_index), FOREIGN KEY(run_id) REFERENCES workflow_runs(run_id) ON DELETE CASCADE
);


-- Model Compare (multi-provider): owner-scoped run history and prompt-free call telemetry.
CREATE TABLE IF NOT EXISTS compare_runs (
  run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('compare','ask')),
  prompt TEXT NOT NULL, evidence_mode TEXT NOT NULL, model_ids_json TEXT NOT NULL,
  payload_json TEXT NOT NULL, idempotency_key TEXT, created_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_compare_runs_user_time ON compare_runs(user_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_compare_runs_idem ON compare_runs(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS provider_call_log (
  call_id TEXT PRIMARY KEY, run_id TEXT, user_id TEXT NOT NULL, request_id TEXT,
  provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, error_category TEXT,
  attempts INTEGER NOT NULL DEFAULT 1, latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER,
  cost_usd REAL, cost_basis TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_call_log_time ON provider_call_log(created_at);
