"""One turn at a time per conversation, in the core, for every surface."""

import asyncio

from chatbot import core


def test_turns_of_one_thread_never_overlap_but_other_threads_run_in_parallel(monkeypatch):
    events: list[str] = []

    async def fake_locked(user_id, thread_id, text, generate, reply, at, request_id=None):
        events.append(f"start {thread_id}:{text}")
        await asyncio.sleep(0.05)
        events.append(f"end {thread_id}:{text}")
        return text

    monkeypatch.setattr(core, "_handle_locked", fake_locked)

    async def run():
        return await asyncio.gather(
            core.send_async("me", "A", "slow"),
            core.send_async("me", "A", "fast"),
            core.send_async("me", "B", "other"),
        )

    replies = asyncio.run(run())

    assert replies == ["slow", "fast", "other"]  # each caller got its own reply
    assert events.index("end A:slow") < events.index("start A:fast")  # same thread: in order
    assert events.index("start B:other") < events.index("end A:slow")  # other thread: parallel
