"""Contract tests for docs/MEMORY_CONTRACTS.md (storage layer)."""

import threading

import pytest

from chatbot import core
from chatbot.memory import messages as store
from chatbot.memory import records, refs, search
from chatbot.memory.db import session
from chatbot.memory.migrate import migrate_facts
from chatbot.memory.models import MemoryRecord, RecordSource
from chatbot.memory.records import MemoryAccessError, MemoryCaller

A = MemoryCaller("me", "A")
B = MemoryCaller("me", "B")
OTHER_USER = MemoryCaller("you", "A")


def say(caller: MemoryCaller, text: str) -> str:
    """Ingest a user message into the caller's session; return its msg ref."""
    core.ingest(caller.user_id, caller.session_id, text, reply="ok")
    user_rows = [m for m in store.thread_messages(caller.session_id) if m.role == "user"]
    return refs.msg(user_rows[-1].id)


def remember(caller, text, *, store_="long_term", kind="fact", key="topic", sources=None, **kw):
    sources = sources or [say(caller, text)]
    return records.write(caller, store=store_, kind=kind, key=key, text=text, sources=sources, **kw)


# --- ownership ---------------------------------------------------------------------------------


def test_session_records_are_visible_only_in_their_session():
    r = remember(A, "Working on the Q3 deck today.", store_="session", kind="task", key="today")
    assert records.read(A, r.ref).text == "Working on the Q3 deck today."
    for other in (B, OTHER_USER):
        with pytest.raises(MemoryAccessError, match="unknown or not visible"):
            records.read(other, r.ref)


def test_long_term_records_cross_sessions_but_not_their_source_conversation():
    src = say(A, "My manager is Priya Raman.")
    r = remember(A, "The user's manager is Priya Raman.", key="manager", sources=[src])
    seen = records.read(B, r.ref)
    assert seen.text == "The user's manager is Priya Raman."
    assert seen.sources[0].ref == src and "Priya" in seen.sources[0].excerpt  # cited excerpt
    with pytest.raises(MemoryAccessError):
        records.read(B, src)  # but not the session message itself
    with pytest.raises(MemoryAccessError):
        records.read(OTHER_USER, r.ref)


def test_invisible_and_malformed_refs_look_like_missing_ones():
    secret = remember(A, "Session-only note.", store_="session", key="n")
    errors = set()
    for ref in [secret.ref, "rec:999999", "rec:abc", "msg:0", "note:1", "../x"]:
        with pytest.raises(MemoryAccessError) as exc:
            records.read(B, ref)
        errors.add(str(exc.value).split(":")[0])
    assert errors == {"unknown or not visible"}


def test_passages_are_visible_only_to_sessions_that_opened_the_doc():
    from chatbot.capabilities.docs import remember_ref
    from chatbot.memory.models import DocPassage

    with session() as s:
        p = DocPassage(
            doc_id="Q3-plan", sha256="x", idx=0, section="Launch", text="Launch is Sept 29."
        )
        s.add(p)
        s.commit()
        pref = refs.Ref("psg", p.id).__str__()
    with pytest.raises(MemoryAccessError):
        records.read(A, pref)
    remember_ref("A", "Q3-plan")
    assert records.read(A, pref).text == "Launch is Sept 29."
    with pytest.raises(MemoryAccessError):
        records.read(B, pref)


# --- write validation ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"store": "global"}, "invalid store"),
        ({"kind": "rumor"}, "invalid store or kind"),
        ({"key": "!!!"}, "key must be"),
        ({"text": "x" * 501}, "text must be"),
        ({"text": "   "}, "text must be"),
        ({"sources": []}, "cite 1-10"),
        ({"sources": [f"msg:{i}" for i in range(1, 12)]}, "cite 1-10"),
    ],
)
def test_write_rejects_bad_input(kwargs, error):
    src = say(A, "hello")
    args = {"store": "long_term", "kind": "fact", "key": "k", "text": "t", "sources": [src]}
    with pytest.raises(MemoryAccessError, match=error):
        records.write(A, **{**args, **kwargs})


def test_sources_must_be_visible_original_evidence():
    mine = say(A, "I live in Portland.")
    theirs = say(B, "Session B private message.")
    with pytest.raises(MemoryAccessError, match="not visible"):
        remember(A, "x", sources=[theirs])
    base = remember(A, "The user lives in Portland.", key="home_city", sources=[mine])
    with pytest.raises(MemoryAccessError, match="original source"):
        remember(A, "Restated.", key="home_city2", sources=[base.ref])  # a record alone
    ok = remember(A, "Portland, Oregon.", key="home_city3", sources=[base.ref, mine])
    assert [x.ref for x in ok.sources] == [base.ref, mine]


# --- versions -----------------------------------------------------------------------------------


def test_explicit_versions_form_a_chain_without_forks():
    v1 = remember(A, "Use British spelling.", kind="preference", key="spelling")
    v2 = remember(A, "Use American spelling.", kind="preference", key="spelling", supersedes=v1.ref)
    chain = records.versions(B, v2.ref)
    assert [v.text for v in chain] == ["Use British spelling.", "Use American spelling."]
    assert [v.current for v in chain] == [False, True]
    assert chain[0].superseded_by == v2.ref and chain[1].supersedes == v1.ref
    assert records.versions(B, v1.ref) == chain  # same chain from either end

    with pytest.raises(MemoryAccessError, match=f"not the current version \\(current: {v2.ref}\\)"):
        remember(A, "Use Canadian spelling.", kind="preference", key="spelling", supersedes=v1.ref)
    with pytest.raises(MemoryAccessError, match="keep its store and kind"):
        remember(A, "x", kind="fact", key="spelling", supersedes=v2.ref)


def test_same_key_does_not_supersede_implicitly():
    a = remember(A, "Fact one.", key="dup")
    b = remember(A, "Fact two.", key="dup")
    assert records.read(A, a.ref).current and records.read(A, b.ref).current


# --- mutation discipline ------------------------------------------------------------------------


def test_idempotent_writes_return_the_original_record():
    src = say(A, "My partner is Sam.")
    first = remember(A, "Partner: Sam.", key="partner", sources=[src], idempotency_key="call-1")
    again = remember(A, "Partner: Sam!!", key="partner", sources=[src], idempotency_key="call-1")
    assert again.ref == first.ref and again.text == "Partner: Sam."
    with session() as s:
        assert len(s.exec(MemoryRecord.__table__.select()).all()) == 1
    with pytest.raises(MemoryAccessError, match="idempotency key already used"):
        remember(OTHER_USER, "x", idempotency_key="call-1")


def test_concurrent_updates_of_one_version_cannot_fork():
    v1 = remember(A, "Tone: formal.", kind="preference", key="tone")
    src = say(A, "change tone")
    results: list[str] = []

    def update(i: int) -> None:
        try:
            r = records.write(
                A,
                store="long_term",
                kind="preference",
                key="tone",
                text=f"Tone: v{i}.",
                sources=[src],
                supersedes=v1.ref,
            )
            results.append(r.ref)
        except MemoryAccessError:
            results.append("rejected")

    threads = [threading.Thread(target=update, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("rejected") == 7
    chain = records.versions(A, v1.ref)
    assert len(chain) == 2


def test_record_and_index_row_commit_together(monkeypatch):
    src = say(A, "hello")

    def boom(conn, record):
        raise RuntimeError("index write failed")

    monkeypatch.setattr(search, "index_record", boom)
    with pytest.raises(RuntimeError):
        remember(A, "Never stored.", key="atomic", sources=[src])
    with session() as s:
        assert s.exec(MemoryRecord.__table__.select()).all() == []
        assert s.exec(RecordSource.__table__.select()).all() == []


# --- search -------------------------------------------------------------------------------------


def test_search_filters_by_owner_and_store_before_ranking():
    remember(A, "Saffron Table caters the farewell.", store_="session", key="caterer")
    remember(B, "Saffron Table also caters in B.", store_="session", key="caterer")
    lt = remember(A, "Saffron Table is the user's favourite caterer.", key="fav_caterer")

    hits_a = records.search_memory(A, "Saffron caterer", store="both")
    texts = [h.excerpt for h in hits_a]
    assert not any("in B" in t for t in texts)  # B's session record and messages excluded
    assert any(h.ref == lt.ref for h in hits_a)
    assert all(h.type in {"record", "message"} for h in hits_a)

    only_lt = records.search_memory(B, "Saffron caterer", store="long_term")
    assert [h.ref for h in only_lt] == [lt.ref]
    assert records.search_memory(OTHER_USER, "Saffron caterer") == []


def test_search_marks_superseded_versions():
    v1 = remember(A, "Use British spelling everywhere.", kind="preference", key="spelling")
    remember(
        A, "Use American spelling everywhere.", kind="preference", key="spelling", supersedes=v1.ref
    )
    hits = {h.ref: h for h in records.search_memory(A, "spelling", store="long_term")}
    assert hits[v1.ref].current is False
    assert sum(h.current for h in hits.values()) == 1


def test_expand_returns_bounded_neighbours_in_the_same_session():
    for i in range(10):
        say(A, f"message {i}")
    say(B, "other session")
    mid = [m for m in store.thread_messages("A")][9]
    around = records.expand(A, refs.msg(mid.id), before=50, after=50)
    assert len(around) == 11  # clamped to 5 + 1 + 5
    assert all(records.read(A, v.ref) for v in around)
    with pytest.raises(MemoryAccessError):
        records.expand(B, refs.msg(mid.id))


# --- repair and migration -----------------------------------------------------------------------


def test_reindex_repairs_messages_missing_from_indexes():
    src = say(A, "The tasting is at Saffron Table on June 18.")
    from chatbot.memory.db import get_engine

    with get_engine().begin() as conn:
        conn.exec_driver_sql("DELETE FROM message_fts")
    assert records.search_memory(A, "tasting Saffron", store="session") == []
    fixed = search.reindex_missing()
    assert fixed["message_fts"] >= 2  # user message + reply
    assert records.search_memory(A, "tasting Saffron", store="session")[0].ref == src
    assert search.reindex_missing()["message_fts"] == 0  # idempotent


def legacy_fact(scope, key: str, text: str, turn: int) -> None:
    """Write a legacy Fact row the way the old extractor did (supersede by scope + key)."""
    from chatbot.memory.models import Fact, FactKind, Scope, utcnow

    with session() as s:
        thread = "A" if scope == Scope.THREAD else None
        current = s.exec(
            Fact.__table__.select().where(
                Fact.key == key, Fact.scope == scope, Fact.superseded_at.is_(None)
            )
        ).first()
        new = Fact(
            user_id="me",
            scope=scope,
            thread_id=thread,
            key=key,
            text=text,
            kind=FactKind.PREFERENCE,
            source_thread_id="A",
            source_turn=turn,
        )
        s.add(new)
        s.flush()
        if current is not None:
            old = s.get(Fact, current.id)
            old.superseded_at, old.superseded_by = utcnow(), new.id
            s.add(old)
        s.commit()


def test_fact_migration_is_incremental_and_keeps_chains():
    from chatbot.memory.models import Scope

    say(A, "Always use British spelling.")
    turn = store.thread_messages("A")[-1].turn
    legacy_fact(Scope.USER, "spelling", "Use British spelling.", turn)
    legacy_fact(Scope.THREAD, "tone", "Be terse here.", turn)
    first = migrate_facts()
    assert first == {"migrated": 2, "without_source": 0, "chains_linked": 0}
    assert migrate_facts()["migrated"] == 0  # idempotent

    say(A, "Switch to American spelling.")
    turn2 = store.thread_messages("A")[-1].turn
    legacy_fact(Scope.USER, "spelling", "Use American spelling.", turn2)
    second = migrate_facts()
    assert second == {"migrated": 1, "without_source": 0, "chains_linked": 1}

    hits = records.search_memory(B, "spelling", store="long_term")
    current = next(h for h in hits if h.current)
    chain = records.versions(B, current.ref)
    assert [v.text for v in chain] == ["Use British spelling.", "Use American spelling."]
    assert "British" in records.read(B, chain[0].ref).sources[0].excerpt
    session_hits = records.search_memory(A, "terse", store="session")
    assert session_hits and records.search_memory(B, "terse") == []
