# Add a capability

Each capability is a folder under `src/chatbot/capabilities/`; it is discovered automatically
and loaded upfront.

1. Create `src/chatbot/capabilities/<name>/instructions.md`. It holds the short usage rules
   added to the chat agent's instructions.
2. Create `src/chatbot/capabilities/<name>/__init__.py` with the tool functions and a
   `CAPABILITY`:

```python
from pydantic_ai import RunContext
from chatbot.capabilities.registry import capability


def weather(ctx: RunContext, city: str) -> str:
    """Current weather for a city (the docstring is the tool description the model sees)."""
    return "..."  # keep tool output bounded; never return unbounded text


CAPABILITY = capability("weather", "Look up the weather.", __file__, weather)
```

3. Use `ctx.deps.user_id` / `ctx.deps.thread_id` for identity, never model arguments. If the
   tool mutates something, make it idempotent per turn (see `capabilities/memory.mutation_key`).
   If a model call is needed, build it with `agent.models.build_model` so it stays under the
   budget guard, and tag it with `call_context(purpose=...)`.
4. Check the per-request token cost, then add tests next to `tests/test_capabilities.py`:

```bash
uv run chatbot capabilities
```

Hard output rules belong in code, not prompts: see `capabilities/linkedin/validator.py`.
