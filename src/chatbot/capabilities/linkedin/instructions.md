LinkedIn posts: to write one, first gather the facts it needs (e.g. `fetch_doc` / `search_doc`
for a document the user names), then call `write_linkedin_post(brief)` with a factual brief:
the announcement, key facts and dates verbatim, audience and any tone the user asked for.
The tool enforces the rules (<= 1,300 characters, <= 3 hashtags at the end, no links) and its
post is shown to the user exactly as returned, so don't rewrite it in your reply.
