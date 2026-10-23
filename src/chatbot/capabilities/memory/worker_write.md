Task: keep memory correct after the user stated or changed something. The task says what is now true and whether it is long-term or only for this conversation.
1. Look for records on the topic first (`list_records`, `search`): the same fact may exist under another key or wording.
2. A current record already says it: unchanged. A current record is out of date or partly overlaps: `revise` it with the complete new truth (the old version stays as history); revise each outdated one. Nothing on the topic: `save`.
3. Preferences (kind preference) are injected into every chat request and capped in total size.
   Write each as one short imperative line. If the limit refuses a write, shorten or merge rules,
   or `retire` an obsolete rule, then retry. Also `retire` preferences the user drops.
4. Write one self-contained sentence (at most 500 characters), preserving names, numbers and
   dates verbatim. Use kind fact | preference | task and a short topic key. Durable facts and
   standing preferences are long_term; conversation-only information is session. Skip small talk
   and instructions for a single piece of work.
5. The user's current message is cited automatically.

Output: status saved | revised | retired | unchanged | skipped; summary: what memory says now and what it replaced.
