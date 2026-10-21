"""Generate the document corpus that `fetch_doc` reads (into DOCS_DIR, default .state/docs).

uv run python scripts/gen_docs.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for `scenarios`

from chatbot.config import get_settings  # noqa: E402
from scenarios.fixtures.docs import write_all  # noqa: E402

if __name__ == "__main__":
    for path in write_all(get_settings().docs_path):
        print(f"wrote {path} ({len(path.read_text(encoding='utf-8')):,} chars)")
