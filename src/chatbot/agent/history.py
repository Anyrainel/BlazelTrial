"""History processor: keep every request of a turn under budget, including tool-loop requests.

Context assembly sizes the first request of a turn. Tool results (a doc note, search passages)
then grow later requests of the same run. Before each request, if the estimate exceeds the
target, the oldest replayed turns are dropped whole (so a tool call never loses its return);
the current turn is never touched. The budget guard on the model remains the hard stop.
"""

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from chatbot.agent.budget import estimate_tokens
from chatbot.config import get_settings


def _turn_starts(messages: list[ModelMessage]) -> list[int]:
    """Indexes where a turn begins: a request carrying a user prompt."""
    return [
        i
        for i, m in enumerate(messages)
        if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts)
    ]


def fit_history(ctx: RunContext, messages: list[ModelMessage]) -> list[ModelMessage]:
    settings = get_settings()
    target = settings.context_target_tokens
    overhead = settings.tool_overhead_tokens
    cpt = settings.estimate_chars_per_token
    params = ModelRequestParameters()

    def over(msgs: list[ModelMessage]) -> bool:
        return estimate_tokens(msgs, params, cpt) + overhead > target

    starts = _turn_starts(messages)
    current = next(
        (i for i in starts if getattr(messages[i], "run_id", None) == ctx.run_id),
        starts[-1] if starts else 0,
    )
    drop = 0
    while over(messages[drop:]):
        later = [i for i in starts if drop < i <= current]
        if not later:
            break  # only the current turn is left; nothing more we may drop
        drop = later[0]
    return messages[drop:] if drop else messages
