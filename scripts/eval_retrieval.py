"""Retrieval recall@k for memory search configurations (PLAN sequence 6).

Paraphrased questions about the synthetic Slack history; a question counts as recalled when a
top-k hit is (or, for notes, covers) a message containing the expected marker. No LLM calls.

    STATE_DIR=<a state with the 500-turn history> uv run python scripts/eval_retrieval.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatbot.config import get_settings  # noqa: E402
from chatbot.memory import records  # noqa: E402
from chatbot.memory.records import MemoryCaller  # noqa: E402

QUESTIONS = [
    ("who is my partner", "partner Sam"),
    ("what's my partner called", "partner Sam"),
    ("which company is catering Dana's farewell", "Saffron Table"),
    ("Dana's goodbye party food vendor", "Saffron Table"),
    ("how much was the deposit for the farewell", "$340"),
    ("when is the food tasting", "tasting"),
    ("when do I get my teeth cleaned", "dentist"),
    ("what book have I been reading", "Started reading"),
    ("what did I eat at midday", "for lunch"),
    ("how long did the morning sync take", "standup"),
    ("remind me what to bring for my workout", "pack shoes"),
    ("who is out ill", "out sick"),
    ("tips for shorter presentations", "slides"),
    ("how to turn down an extra meeting", "say no"),
]

CONFIGS = {
    "bm25 raw": dict(sources=("messages",)),
    "bm25 raw+notes": dict(sources=("messages", "notes")),
    "raw+notes+vec@0.35": dict(sources=("messages", "notes", "vectors"), cutoff=0.35),
    "raw+notes+vec@0.48": dict(sources=("messages", "notes", "vectors"), cutoff=0.48),
}


def recalled(caller: MemoryCaller, hits, marker: str) -> bool:
    for h in hits:
        view = records.read(caller, h.ref)
        if h.type == "message" and marker.lower() in view.text.lower():
            return True
        if h.type == "note":
            for ref in view.covers:
                if marker.lower() in records.read(caller, ref).text.lower():
                    return True
    return False


def main(k: int = 5) -> None:
    caller = MemoryCaller(get_settings().default_user_id, "slack-dm")
    print(f"recall@{k} over {len(QUESTIONS)} paraphrased questions\n")
    for name, cfg in CONFIGS.items():
        if "cutoff" in cfg:
            os.environ["RETRIEVAL_MAX_DISTANCE"] = str(cfg["cutoff"])
            get_settings.cache_clear()
        found = 0
        misses = []
        for question, marker in QUESTIONS:
            hits = records.search_memory(
                caller, question, store="session", k=k, sources=cfg["sources"]
            )
            if recalled(caller, hits, marker):
                found += 1
            else:
                misses.append(question)
        print(f"{name:<20} {found:>2}/{len(QUESTIONS)}   misses: {misses}")


if __name__ == "__main__":
    main()
