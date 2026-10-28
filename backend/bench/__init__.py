"""AtlasBench (M8): reproducible evaluation of research agents/models.

Import-light on purpose: importing this package loads no embedding model, vector
database, LLM client or network client. Submodules:

``models``    task / config definitions, vocabulary, limits and content hashing
``suites``    the versioned built-in benchmark suite (frozen evidence corpora + gold)
``metering``  token / latency / cost accounting around any chat model
``frozen``    the frozen "world": the M4 tool registry wired to a task's corpus
``runner``    runs one (task, config) pair through the real tool registry
``scoring``   pure, deterministic metrics (no LLM judge, no clock, no I/O)
``compare``   pure aggregation and run-to-run comparison
``repository``/``service``  user-scoped persistence, state machine and orchestration
"""
