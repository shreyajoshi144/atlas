"""Multi-provider LLM layer (OpenAI, Anthropic, Google Gemini, xAI Grok).

Import-light on purpose: nothing here imports FastAPI, LangChain or the app's
Settings, so the provider layer can be unit-tested (and reused) on its own.
Keys come from the server environment only and are never serialized.
"""
