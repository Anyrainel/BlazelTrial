"""Turn search hits into a bounded block of earlier-conversation snippets."""

from chatbot.config import get_settings
from chatbot.memory import messages as store
from chatbot.memory import search
from chatbot.memory.models import Role


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + " …"


def snippets(thread_id: str, query: str, *, before_turn: int | None = None) -> str:
    """Formatted earlier turns relevant to `query`, oldest first, within the retrieval budget.

    Empty string when nothing relevant precedes `before_turn`.
    """
    settings = get_settings()
    if before_turn is not None and before_turn <= 1:
        return ""
    hits = search.search(
        thread_id,
        query,
        k=settings.retrieval_top_k,
        before_turn=before_turn,
        max_distance=settings.retrieval_max_distance,
    )
    if not hits:
        return ""
    by_turn = store.turns(thread_id, [h.turn for h in hits])
    budget_chars = int(settings.retrieval_token_budget * settings.estimate_chars_per_token)
    blocks: dict[int, str] = {}
    used = 0
    for hit in hits:  # best first, so the budget keeps the strongest matches
        rows = by_turn.get(hit.turn, [])
        if not rows:
            continue
        lines = [f"[turn {hit.turn}, {rows[0].created_at:%Y-%m-%d}]"]
        for r in rows:
            who = "user" if r.role == Role.USER else "assistant"
            lines.append(f"{who}: {_clip(r.text, settings.retrieval_snippet_chars)}")
        block = "\n".join(lines)
        if used + len(block) > budget_chars:
            break
        blocks[hit.turn] = block
        used += len(block)
    return "\n\n".join(blocks[t] for t in sorted(blocks))
