Task: keep memory correct after the user stated or changed something. The task says what is now true and whether it is long-term or only for this conversation.
1. Look for records on the topic first (`list_records`, `search`): the same fact may exist under another key or wording.
2. A current record already says it: unchanged. A current record is out of date or partly overlaps: `revise` it with the complete new truth (the old version stays as history); revise each outdated one. Nothing on the topic: `save`.
3. Preferences (kind preference) are copied into every request the assistant makes, and their total size is capped: write each as one short imperative line (for example "Use American spelling."), never a paragraph. If a write is refused for the limit, shorten it, merge it with a related preference, or `retire` one that no longer matters, then write again. `retire` also removes a preference the user dropped.
4. Write one self-contained sentence (at most 500 characters) with names, numbers and dates verbatim; kind fact | preference | task; a short key such as "manager" or "spelling". Durable facts and standing preferences are long_term; what applies only to this conversation is session. A one-off instruction for a single piece of work, or small talk, is not memory: store nothing (skipped).
5. The user's current message is cited automatically.

Output: status saved | revised | retired | unchanged | skipped; summary: what memory says now and what it replaced.
