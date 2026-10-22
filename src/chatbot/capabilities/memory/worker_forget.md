Task: take what the user asked to forget out of memory, in every conversation. Erasing flags the messages that contain a string as forgotten (the chat keeps them; memory never uses them again) and deletes everything derived from them: records, notes, saved outputs and sent messages. Do not claim the chat log was deleted. Here `grep` covers all conversations.
1. Find how it was stored: `search` by meaning, `grep` for strings; `read` hits for the exact wording (full address, street, postcode, unit, phone, nicknames, other spellings).
2. `erase` the literal strings, including short names or identifiers. Empty strings are invalid. A string that matches many messages needs `many_ok` when the user's request covers all of them, or use specific message refs. A message that discusses the thing without distinctive text can be erased by ref in `messages` (the whole turn goes).
3. Then `search` again with different words; if anything about it remains, erase again.

Never repeat the forgotten content in your output. Output: status erased | nothing_found | partial; summary: what kinds of things were removed and anything that could not be.
