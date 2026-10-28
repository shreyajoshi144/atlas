"""Model routing, Cost Autopilot and production fallback (M9).

Sits *behind* ``core.llm.get_chat_llm()``: agents and the graph keep calling
``invoke_with_retry(self.llm, messages)`` exactly as before. Import-light — importing this
package loads no LLM client, database or network library.

``models``    routing policy (explicit | autopilot), vocabulary, limits
``failures``  which provider errors justify a fallback (and which never do)
``context``   who a call is for (ContextVar) and the benchmark switch
``selector``  pure model choice: quality / cost / latency requirements over AtlasBench evidence
``evidence``  reads a user's completed AtlasBench runs as per-model evidence
``repository``user-scoped persistence of policies and per-call usage
``router``    the router + the ``RoutedLLM`` proxy that records every call
``service``   API-facing operations (policies, preview, usage)
"""
