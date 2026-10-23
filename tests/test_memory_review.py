"""Memory review fixes: key guard at any size, record surveys, dated prompts, the user's own
words for writes, role labels and newest-first search."""

from datetime import UTC, datetime

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from chatbot import core
from chatbot.agent.agent import get_agent
from chatbot.agent.models import build_model
from chatbot.capabilities.memory import subagent
from chatbot.capabilities.memory.subagent import Mode, get_worker
from chatbot.config import get_settings
from chatbot.memory import messages as store
from chatbot.memory import records
from chatbot.memory.records import MemoryCaller
from tests.fakes import search_agent_answering, worker_answering

A = MemoryCaller("me", "A")


def _source() -> str:
    """A visible original message to cite."""
    core.ingest("me", "A", "Seed message for citations.", "ok")
    return f"msg:{store.thread_messages('A')[0].id}"


def _write(key: str, text: str, src: str, store_: str = "long_term", kind: str = "fact"):
    return records.write(A, store=store_, kind=kind, key=key, text=text, sources=[src])


# --- the duplicate-key guard and surveys do not depend on how many records exist -----------------


def test_save_refuses_a_duplicate_key_even_when_many_newer_records_exist():
    src = _source()
    _write("manager", "The user's manager is Priya Raman.", src)
    for i in range(records.MAX_K + 5):  # pushes "manager" out of the newest ten
        _write(f"topic_{i}", f"Fact number {i}.", src)
    assert all(v.key != "manager" for v in records.list_current(A, k=records.MAX_K))

    again = {"store": "long_term", "kind": "fact", "key": "Manager", "text": "Manager: Priya."}
    seen: list[str] = []
    final = {"status": "unchanged", "summary": "-"}
    with worker_answering(Mode.WRITE, final, steps=[("save", again)], seen=seen):
        core._run_sync(subagent.work(Mode.WRITE, A, "Memory update: manager"))
    assert seen[0].startswith("Not saved:") and "already the current 'Manager'" in seen[0]
    assert sum(v.key == "manager" for v in records.list_current(A, k=records.MAX_LIST)) == 1


def test_key_guard_respects_store_kind_and_session_visibility():
    src = _source()
    _write("tone", "Keep replies terse here.", src, store_="session", kind="preference")
    other = MemoryCaller("me", "B")
    assert records.current_with_key(A, store="session", kind="preference", key="tone")
    assert records.current_with_key(other, store="session", kind="preference", key="tone") is None
    assert records.current_with_key(A, store="long_term", kind="preference", key="tone") is None
    assert records.current_with_key(A, store="session", kind="fact", key="tone") is None


def test_list_records_surveys_dozens_with_shortened_text():
    src = _source()
    for i in range(30):
        _write(f"topic_{i}", f"Topic {i}: " + "word " * 80, src)
    seen: list[str] = []
    final = {"status": "unchanged", "summary": "-"}
    with worker_answering(Mode.WRITE, final, steps=[("list_records", {"k": 40})], seen=seen):
        core._run_sync(subagent.work(Mode.WRITE, A, "Memory update: topics"))
    lines = seen[0].splitlines()
    assert len(lines) == 30 and any("'topic_0'" in line for line in lines)  # the oldest too
    assert all(len(line) < 260 for line in lines)  # the 400-character texts were cut


# --- dates and the user's own words --------------------------------------------------------------


def _capture_prompts(final: dict, prompts: list[str]):
    def fn(messages, info: AgentInfo) -> ModelResponse:
        first = messages[0]
        assert isinstance(first, ModelRequest)
        prompts.extend(str(p.content) for p in first.parts if isinstance(p, UserPromptPart))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, final)])

    return build_model(get_settings(), raw=FunctionModel(fn))


def test_write_worker_gets_today_and_the_users_actual_message(monkeypatch):
    monkeypatch.setattr(records, "today", lambda: "2030-01-02")
    prompts: list[str] = []
    model = _capture_prompts({"status": "skipped", "summary": "-"}, prompts)
    core.ingest("me", "A", "Dentist is next Friday, and I might switch jobs.", "ok")
    ref = f"msg:{store.thread_messages('A')[0].id}"
    with get_worker(Mode.WRITE).override(model=model):
        core._run_sync(subagent.work(Mode.WRITE, A, "Memory update: dentist", current_msg_ref=ref))
    [prompt] = prompts
    assert prompt.startswith("Today: 2030-01-02")
    assert "Dentist is next Friday, and I might switch jobs." in prompt  # not the model's summary


def test_forgotten_message_text_is_not_shown_to_the_write_worker():
    core.ingest("me", "A", "Secret code is 4471-X.", "ok")
    [msg, _] = store.thread_messages("A")
    from chatbot.memory import forget

    forget.forget_values("me", ["4471-X"])
    assert subagent._current_message_text(f"msg:{msg.id}") is None


def test_read_worker_and_chat_agent_state_the_date(monkeypatch):
    monkeypatch.setattr(records, "today", lambda: "2030-01-02")
    prompts: list[str] = []
    model = _capture_prompts({"status": "not_found", "answer": "-"}, prompts)
    with get_worker(Mode.READ).override(model=model):
        core._run_sync(subagent.ask(A, "Who is my manager?"))
    assert prompts[0].startswith("Today: 2030-01-02\nQuestion:")

    requests: list = []

    def fn(messages, info: AgentInfo) -> ModelResponse:
        requests.append(list(messages))
        return ModelResponse(parts=[TextPart("ok")])

    with get_agent().override(model=build_model(get_settings(), raw=FunctionModel(fn))):
        core.send("me", "B", "hello")
    instructions = requests[0][-1].instructions or ""
    assert instructions.endswith("Today's date: 2030-01-02.")


# --- roles and recency in search ---------------------------------------------------------------


def _ingest_dated() -> None:
    day = lambda d: datetime(2026, 9, d, 12, tzinfo=UTC)  # noqa: E731
    core.ingest("me", "A", "My manager is Priya Raman.", "Noted, Priya Raman.", day(1))
    core.ingest("me", "A", "Filler about lunch.", "Fine.", day(10))
    core.ingest("me", "A", "Marco Chen is my new manager.", "Noted, Marco Chen.", day(20))


def test_message_hits_carry_the_role_and_the_worker_sees_it():
    _ingest_dated()
    hits = [h for h in records.search_memory(A, "Priya Raman manager") if h.type == "message"]
    assert hits and all(h.role in ("user", "assistant") for h in hits)
    assert "by user" in subagent._fmt_hits(hits) or "by assistant" in subagent._fmt_hits(hits)
    records_only = records.search_memory(A, "manager", sources=("records",))
    assert all(h.role is None for h in records_only)


def test_newest_first_orders_hits_by_date_without_changing_which_are_found():
    _ingest_dated()
    plain = records.search_memory(A, "manager", sources=("messages",), k=5)
    newest = records.search_memory(A, "manager", sources=("messages",), k=5, newest_first=True)
    assert {h.ref for h in plain} == {h.ref for h in newest}
    assert [h.created_at for h in newest] == sorted((h.created_at for h in newest), reverse=True)
    assert "Marco Chen" in newest[0].excerpt

    seen: list[str] = []
    steps = [("search", {"query": "manager", "newest_first": True})]
    with search_agent_answering({"status": "not_found", "answer": "-"}, steps, seen):
        core._run_sync(subagent.ask(A, "Who is my manager?"))
    assert seen[0].splitlines()[0].count("Marco") == 1
