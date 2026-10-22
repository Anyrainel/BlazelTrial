"""Last-resort request shrinking: make an oversized request fit instead of failing the user.

Context assembly keeps requests small on purpose; this is for when the provider's own count says
a request is still too big (a pasted wall of text, tool results that grew, an optimistic
estimate). It only changes the copy that is sent, never stored history, and never splits a tool
call from its return. Cheapest loss first:
  1. cut the middle out of the largest tool results;
  2. drop the oldest whole turns (never the current one);
  3. replace older tool results by a stub;
  4. cut the middle out of the current user message (the full text stays in the database);
  5. cut tool results down to a few hundred characters.
"""

from collections.abc import Callable
from dataclasses import replace

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ToolReturnPart,
    UserPromptPart,
)

Size = Callable[[list[ModelMessage]], int]

_RETURN_FLOORS = (1500, 400)
_PROMPT_FLOOR = 800
_STUB = "[tool result omitted to fit the context window]"


def clip_middle(text: str, limit: int) -> str:
    """`text` cut to about `limit` characters, keeping its start and end."""
    if len(text) <= limit:
        return text
    # The marker consumes part of the limit too. Otherwise clipping near a floor
    # can grow the text and prevent the shrinking loop from making progress.
    omitted = len(text) - limit
    marker = f"\n…[{omitted} characters omitted to fit the context window]…\n"
    if len(marker) >= limit:
        return text[:limit]
    keep = limit - len(marker)
    head = int(keep * 0.6)
    tail = keep - head
    return text[:head] + marker + text[-tail:]


def _returns(messages: list[ModelMessage]) -> list[tuple[int, int]]:
    return [
        (i, j)
        for i, m in enumerate(messages)
        if isinstance(m, ModelRequest)
        for j, p in enumerate(m.parts)
        if isinstance(p, ToolReturnPart)
    ]


def _set_part(messages: list[ModelMessage], i: int, j: int, part) -> None:
    parts = list(messages[i].parts)
    parts[j] = part
    messages[i] = replace(messages[i], parts=parts)


def _truncate_returns(messages: list[ModelMessage], size: Size, target: int, floor: int) -> None:
    """Halve the largest tool results (not below `floor` characters) until the size fits."""
    while size(messages) > target:
        sized = [
            (len(messages[i].parts[j].model_response_str()), i, j) for i, j in _returns(messages)
        ]
        length, i, j = max(sized, default=(0, 0, 0))
        if length <= floor:
            return
        part = messages[i].parts[j]
        clipped = clip_middle(part.model_response_str(), max(floor, length // 2))
        if len(clipped) >= length:
            return  # the omission marker would not make it shorter
        _set_part(messages, i, j, replace(part, content=clipped))


def _drop_old_turns(messages: list[ModelMessage], size: Size, target: int) -> list[ModelMessage]:
    starts = [
        i
        for i, m in enumerate(messages)
        if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts)
    ]
    out = messages
    for start in starts[1:]:  # keep the last turn: the oldest start is dropped first
        if size(out) <= target:
            break
        out = messages[start:]
    return out


def _stub_returns(messages: list[ModelMessage], size: Size, target: int) -> None:
    for i, j in _returns(messages)[:-1]:  # oldest first; the latest result stays
        if size(messages) <= target:
            return
        part = messages[i].parts[j]
        _set_part(messages, i, j, replace(part, content=_STUB))


def _truncate_prompt(messages: list[ModelMessage], size: Size, target: int) -> None:
    """Cut the middle out of the last user message; its text may sit beside a cache marker."""
    found = [
        (i, j)
        for i, m in enumerate(messages)
        if isinstance(m, ModelRequest)
        for j, p in enumerate(m.parts)
        if isinstance(p, UserPromptPart)
    ]
    if not found:
        return
    i, j = found[-1]
    part = messages[i].parts[j]
    items = [part.content] if isinstance(part.content, str) else list(part.content)
    while size(messages) > target:
        texts = [(len(x), k) for k, x in enumerate(items) if isinstance(x, str)]
        length, k = max(texts, default=(0, 0))
        if length <= _PROMPT_FLOOR:
            return
        clipped = clip_middle(items[k], max(_PROMPT_FLOOR, length // 2))
        if len(clipped) >= length:
            return
        items[k] = clipped
        content = items[0] if isinstance(part.content, str) else list(items)
        _set_part(messages, i, j, replace(part, content=content))


def shrink_messages(messages: list[ModelMessage], size: Size, target: int) -> list[ModelMessage]:
    """A copy of `messages` reduced until `size(copy) <= target`, or as small as the steps allow."""
    out = list(messages)
    if size(out) <= target:
        return out
    _truncate_returns(out, size, target, _RETURN_FLOORS[0])
    out = _drop_old_turns(out, size, target)
    _stub_returns(out, size, target)
    _truncate_prompt(out, size, target)
    _truncate_returns(out, size, target, _RETURN_FLOORS[1])
    return out
