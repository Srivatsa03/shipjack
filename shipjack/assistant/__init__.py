"""The shopping assistant: a LangGraph multi-agent app on the agent-platform side of a shipjack store.

The store (``shipjack-serve``) is the merchant. This package is the other side, the AI platform a
shopper talks to. It reaches the store the way a real platform would: product discovery over the
store's MCP server, checkout and orders over its ACP REST API, order updates through signed webhooks.

* ``store_client``: the MCP and REST clients, with retries and idempotency keys.
* ``models``: chat models (Gemini, Groq) with a fallback, and a scripted model for tests.
* ``graph``: the agents and the approval steps (LangGraph).
* ``api``: the FastAPI service the web app calls (``shipjack-assistant``).
"""
