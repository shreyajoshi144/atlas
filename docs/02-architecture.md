# Architecture

Atlas is a single FastAPI service with a dependency-free static frontend, two stores (SQLite and ChromaDB) and a set of external model and search providers.

## 1. System context

```mermaid
flowchart LR
    U([User]) --> UI

    subgraph Browser
        UI["Static frontend<br/>HTML · CSS · vanilla JS"]
    end

    subgraph API["FastAPI service  ·  /api/*"]
        direction TB
        MW["Middleware<br/>request context · body-size cap<br/>security headers · CORS"]
        AUTH["Auth<br/>Argon2id · signed tokens · rate limits"]
        RES["Research graph<br/>LangGraph"]
        KNOW["Knowledge · Retrieval · Challenges"]
        EVAL["Compare · Playground · AtlasBench"]
        ROUTE["Routing · Budgets · Ops"]
        MW --> AUTH --> RES & KNOW & EVAL & ROUTE
    end

    subgraph Storage
        SQL[("SQLite<br/>users · reports · claims<br/>bench runs · budgets")]
        VEC[("ChromaDB<br/>report chunks")]
    end

    subgraph External
        GROQ[Groq]
        OAI[OpenAI]
        ANT[Anthropic]
        GEM[Gemini]
        XAI[xAI]
        TAV[Tavily search]
        WEB[Public web pages]
    end

    EMB["all-MiniLM-L6-v2, local<br/>ONNX in the container"]

    UI <-->|"JSON + bearer token"| MW
    RES --> ROUTE --> GROQ
    EVAL --> OAI & ANT & GEM & XAI & GROQ
    RES --> TAV
    RES --> WEB
    API --> SQL
    KNOW --> VEC --> EMB
```

**Why two stores.** The data is read in two different ways. SQLite holds structured, relational records. ChromaDB holds embedded text chunks for similarity search. Neither duplicates the other, and both live under one data directory so a single volume holds all state.

**Why a static frontend.** No build step and no framework means the whole app ships as one container and the API can serve the UI from the same origin, which removes CORS from the deployed path.

## 2. Research pipeline

The pipeline is a LangGraph state machine. A plan is validated and executed over a closed tool registry. If planning fails, the run falls back to the fixed linear pipeline.

```mermaid
flowchart TD
    Q([Research question]) --> I[initialize]
    I --> P[plan]
    P --> X[agentic_execution]
    X -->|plan succeeded| W
    X -->|fallback| S[search]
    S --> SC[scrape]
    SC --> E[evidence_extraction]
    E --> V[verification]
    V --> W[writer]
    W --> C[credibility]
    C --> B[executive_brief]
    B --> F[finalize_report]
    F --> D([Research case stored])

    S -.-> T[(Tavily)]
    SC -.-> SSRF{{SSRF guard}} -.-> H[aiohttp + BeautifulSoup]
    V -.-> L[(LLM)]
    W -.-> L
    B -.-> L
```

### Agentic execution

```mermaid
flowchart LR
    PL[Planner] --> VAL{Plan valid?}
    VAL -->|no| FB[Fixed pipeline fallback]
    VAL -->|yes| EX[Execute steps]
    EX --> T["Closed tool registry<br/>search_web · scrape_source · search_existing_evidence<br/>verify_claim · request_more_evidence"]
    T --> CHK{Verification passed?}
    CHK -->|yes| DONE[Hand off to writer]
    CHK -->|no, replans left| RP[Re-plan<br/>max 3]
    RP --> EX
    CHK -->|no, none left| DONE
```

The tool registry is a closed set: only named tools can exist, each is registered once, and there is no `register` method after construction, so a model can't invent a tool. Retrieved text is wrapped as untrusted data before any model sees it.

## 3. Retrieval

Atlas searches past research with two retrievers merged by reciprocal-rank fusion, then reranks.

```mermaid
flowchart LR
    Qy([Query + filters]) --> BM[BM25<br/>lexical]
    Qy --> DN[Dense<br/>cosine similarity]
    BM --> RRF["Reciprocal-rank fusion<br/>k = 60"]
    DN --> RRF
    RRF --> RR["Feature reranker<br/>query coverage · phrase match<br/>title match · dense score"]
    RR --> MMR["MMR diversification<br/>λ = 0.7"]
    MMR --> OUT([Ranked passages])
```

Defaults: 50 candidates per first-stage retriever, 30 passed to the reranker. Every query is scoped to one user and can't read another user's chunks. The reranker is a protocol, so a cross-encoder can replace it without touching the retriever.

## 4. Model routing

Agents call the language model through one function. The router sits behind it, so agent code is unchanged by routing.

```mermaid
flowchart TD
    A[Agent calls get_chat_llm] --> SC{Call scope}
    SC -->|none / background| APP[Application model<br/>no fallback, not recorded]
    SC -->|AtlasBench run| PIN[Pinned config model<br/>no autopilot, no fallback]
    SC -->|user scope| POL{Active policy}
    POL -->|explicit| EXP[Always the chosen model]
    POL -->|autopilot| AP["Cheapest candidate meeting ALL of:<br/>quality ≥ min · latency ≤ max · cost ≤ max"]
    AP -->|none qualifies| EXP
    EXP --> CALL[Provider call]
    AP --> CALL
    CALL -->|"timeout · 429 · 5xx · unreachable"| FBK{Fallback enabled?}
    FBK -->|yes, up to 3| CALL
    FBK -->|no| ERR[Error to caller]
    CALL -->|"400 · 401 · 403 · 422 · bugs"| ERR
    CALL --> REC[(llm_usage row)]
```

Quality and latency come from the user's own most recent AtlasBench run per model. Unknown is never good enough: a model with no benchmark, price or latency is not eligible.

## 5. Evaluation loop

```mermaid
flowchart LR
    T["Frozen task<br/>fictional evidence + gold claims"] --> R[Run through real<br/>tool registry + verifier]
    CFG["Config<br/>model · strategy"] --> R
    R --> SCORE["Deterministic scoring<br/>6 measures, no LLM judge"]
    SCORE --> ST[(bench_run_cases)]
    ST --> CMP[Compare runs<br/>same task hash only]
    ST --> ARB[Output arbitration]
    ST --> EV[Routing evidence]
    EV --> AUTO[Cost Autopilot]
```

See [Evaluation](03-evaluation.md) for the measures and methodology.

## 6. Data model

```mermaid
erDiagram
    users ||--o{ research_sessions : owns
    users ||--o{ reports : owns
    users ||--o{ notes : writes
    users ||--o{ routing_policies : defines
    users ||--o{ bench_runs : runs
    users ||--o{ llm_usage : generates
    users ||--|| user_usage_limits : "capped by"

    topics ||--o{ reports : groups
    reports ||--o{ sources : cites
    reports ||--o{ report_chunks : "split into"
    reports ||--o{ knowledge_claims : yields

    knowledge_claims ||--o{ knowledge_claim_evidence : "backed by"
    knowledge_claims ||--o{ knowledge_claim_observations : "tracked over time"
    knowledge_claims ||--o{ knowledge_claim_entities : mentions
    knowledge_evidence }o--|| knowledge_sources : "quoted from"
    knowledge_claims ||--o{ challenges : "tested by"
    challenges ||--o{ challenge_attempts : has

    bench_tasks ||--o{ bench_run_cases : "scored in"
    bench_configs ||--o{ bench_runs : "used by"
    bench_runs ||--o{ bench_run_cases : contains
    bench_runs ||--o{ bench_arbitrations : "compared in"

```

Every user-facing table carries a `user_id`, and every repository query filters on it. Tests assert isolation between two users for research, notes, Playground, benchmark runs and routing policies.

## 7. Security and request lifecycle

```mermaid
sequenceDiagram
    participant B as Browser
    participant M as Middleware
    participant A as Auth dependency
    participant R as Route handler
    participant L as Budget and routing
    participant P as Provider

    B->>M: POST /api/research/run (Bearer token)
    M->>M: Reject declared body over limit (413)
    M->>A: Pass through
    A->>A: Verify HMAC signature and expiry
    A->>R: current_user
    R->>L: Check daily limit and monthly cap
    alt Over limit
        L-->>B: 429
    else Within limit
        L->>P: Chosen model call with bounded retry
        P-->>L: Response and usage
        L->>L: Record llm_usage
        L-->>R: Result
        R-->>B: JSON + security headers
    end
```

| Control | Where |
|---|---|
| Argon2id password hashing, PBKDF2 legacy verify and migrate | `backend/auth/security.py` |
| HMAC-SHA256 signed expiring tokens, constant-time compare | `backend/auth/security.py` |
| Per-route rate limits for auth endpoints | `backend/auth/rate_limit.py` |
| Production startup validation (secret strength, CORS allowlist, spend cap) | `backend/core/config.py` |
| SSRF guard on page fetching | `backend/services/ssrf_guard.py` |
| Log redaction for provider token formats | `backend/core/logging.py` |
| Security headers, request-size cap, generic 500 responses | `main.py` |
| Frontend: only `http(s)` URLs reach an `href`, markdown output is escaped first | `frontend/js/script.js` |

## 8. Deployment topology

```mermaid
flowchart LR
    DEV[Developer] -->|git push| GH[GitHub]
    GH -->|Actions| CI["CI<br/>compile · pytest · unit tests<br/>node checks"]
    GH -->|auto-deploy on push| HF

    subgraph HF["Container · port 7860 (Render, or a Hugging Face Docker Space)"]
        SEED[seed_demo.py] --> UV["uvicorn<br/>deploy.space_app"]
        UV --> API2["/api/* , /health , /ready"]
        UV --> STATIC["Static frontend at /"]
        UV --> DATA[("data/<br/>sqlite · chroma · logs")]
    end

    USER([Browser]) -->|HTTPS| HF
    HF --> PROV[Provider APIs]
```

One process serves both the API and the UI, so there is a single origin and no CORS in the deployed path. The image runs as a non-root user, bakes the ONNX embedding model in at build time so startup never needs the network, and has a `HEALTHCHECK` on `/ready`. It sets four environment variables for 512 MB hosts: `ATLAS_EMBEDDING_BACKEND=onnx`, `ATLAS_DENSE_RANKING=0`, `ATLAS_MAX_TOP_K=6` and `ATLAS_LIGHT_MODE=1` (see [configuration](09-configuration.md)).

## 9. Design decisions and trade-offs

| Decision | Reason | Cost |
|---|---|---|
| SQLite and local Chroma | Zero-ops, single volume, easy to reason about | One writer process, no horizontal scale. The next step is Postgres and a hosted vector store. |
| Deterministic scoring, no LLM judge | Reproducible, cheap, explainable | Lexical overlap can miss paraphrased quotes |
| Closed tool registry | A model can't call anything not on the list | Adding a tool is a code change |
| Router behind `get_chat_llm` | Agents and the graph don't change when routing does | Context has to be carried explicitly into thread-pool calls |
| Hand-rolled signed tokens | No extra dependency, simple to audit | No revocation list. A standard JWT library would be the next step. |
| In-memory rate limiter | No Redis dependency | Per process, resets on restart |
| Small-host mode (ONNX embeddings, keyword ranking, at most 6 sources, no vector indexing) | The live demo runs on a free 512 MB instance; the full path peaked at about 870 MB | Lower semantic ranking quality on the demo, and Ask Atlas and Discover do not see new reports there. The full path stays the default everywhere else. |
| Static frontend without a framework | No build step, one container | Large hand-written `script.js` (about 2,100 lines) that would benefit from modules |
