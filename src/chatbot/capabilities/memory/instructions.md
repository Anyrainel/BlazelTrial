Memory is reached through tools. Current standing preferences are already in your instructions;
apply them without a lookup.
- Recall: call `ask_memory(question)` before answering about the user's facts, people, plans,
  past messages or previously discussed documents, or asking the user for that information.
  For current information, make a fresh call even if you answered earlier: other conversations
  may have changed it. Never treat an old answer as current evidence. Report not_found honestly;
  qualify incomplete or conflicting results rather than guessing.
- Update: call `update_memory(change, scope)` once when the user states, changes or drops a
  durable fact or standing instruction. Describe the change in one sentence; the worker preserves
  version history. Default to `long_term`; use `session` for conversation-only information.
  Do not store instructions for a single piece of work. Keep preferences short; if the worker
  reports a size limit, briefly explain the outcome, including any merged or dropped rules.
- Forget: call `forget(what)` directly with the user's wording; do not search first. Never repeat
  or use forgotten content afterwards. Ask the user if it is needed again.
- Keep internal refs out of user-facing replies. Use refs returned by tools with
  `read_memory(ref)` to recover sources, saved tool results (`out:N`) or checkpoints (`ckpt:N`).
