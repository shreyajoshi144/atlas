PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS topics (
    topic_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_sessions (
    session_id TEXT PRIMARY KEY,
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

CREATE INDEX IF NOT EXISTS idx_topics_topic ON topics (topic);
CREATE INDEX IF NOT EXISTS idx_reports_topic_id ON reports (topic_id);
CREATE INDEX IF NOT EXISTS idx_reports_session_id ON reports (session_id);
CREATE INDEX IF NOT EXISTS idx_sources_report_id ON sources (report_id);
CREATE INDEX IF NOT EXISTS idx_sources_session_id ON sources (session_id);
CREATE INDEX IF NOT EXISTS idx_sources_domain ON sources (domain);
CREATE INDEX IF NOT EXISTS idx_sessions_topic_id ON research_sessions (topic_id);
CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON research_sessions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chunks_report_id ON report_chunks (report_id);
CREATE INDEX IF NOT EXISTS idx_chunks_session_id ON report_chunks (session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_topic ON report_chunks (topic);
CREATE INDEX IF NOT EXISTS idx_events_session_id ON analytics_events (session_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON analytics_events (event_type);