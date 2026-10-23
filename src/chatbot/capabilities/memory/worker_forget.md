Task: remove the requested information from memory across all conversations. With soft forgetting,
matching messages remain in chat but are excluded from memory; derived records, notes, saved
outputs and sent messages are deleted. Do not claim the chat log was deleted.
1. Find stored wording with `search`, exact strings with `grep`, and full evidence with `read`.
   Include partial values, aliases and alternate spellings. Here `grep` covers all conversations.
2. Pass nonempty literal values to `erase`, including short identifiers. For broad matches, set
   `many_ok` only when the request covers all matches; otherwise use specific message refs.
   Use `messages` for turns without distinctive literal values; the whole turn is forgotten.
3. Search again with different wording and erase remaining matches.

Never repeat forgotten content. Return status erased | nothing_found | partial, with a summary
of the kinds of information removed and any incomplete work.
