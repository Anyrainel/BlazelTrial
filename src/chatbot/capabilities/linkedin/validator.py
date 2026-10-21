"""LinkedIn post rules, enforced in code: <= 1,300 characters, <= 3 hashtags, all at the end,
no links. `problems()` checks a post; `assemble()` builds one that always passes."""

import re

MAX_CHARS = 1300
MAX_HASHTAGS = 3

_TLDS = "com|org|net|io|ai|co|dev|app|ly|me|us|uk|de|fr|info|biz|tv|gg|xyz|link|page"
LINK = re.compile(
    rf"(https?://\S+|www\.\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:{_TLDS})\b(?:/\S*)?)", re.IGNORECASE
)
HASHTAG = re.compile(r"(?<![\w&#])#(\w+)")


def hashtags(post: str) -> list[str]:
    return HASHTAG.findall(post)


def problems(post: str) -> list[str]:
    """Rule violations of a finished post (empty list = valid)."""
    out = []
    if len(post) > MAX_CHARS:
        out.append(f"{len(post)} characters (max {MAX_CHARS})")
    tags = hashtags(post)
    if len(tags) > MAX_HASHTAGS:
        out.append(f"{len(tags)} hashtags (max {MAX_HASHTAGS})")
    if tags:
        last_line = post.rstrip().splitlines()[-1]
        rest = post.rstrip()[: -len(last_line)]
        if HASHTAG.search(rest) or HASHTAG.sub("", last_line).strip():
            out.append("hashtags must all be at the end, on the last line")
    if LINK.search(post):
        out.append("contains a link")
    return out


def _clean_tag(tag: str) -> str:
    return re.sub(r"[^\w]", "", tag.lstrip("#"))


def _fit(body: str, limit: int) -> str:
    """Trim `body` to `limit` chars, preferring a sentence end, else a word boundary."""
    if len(body) <= limit:
        return body
    cut = body[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(".\n"))
    if end >= limit // 2:
        return cut[: end + 1].rstrip()
    return cut[: cut.rfind(" ")].rstrip(" ,;:-") + "…"


def clean_body(body: str) -> str:
    """Remove links and turn inline hashtags into plain words."""
    body = LINK.sub("", body)
    body = HASHTAG.sub(lambda m: m.group(1), body)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r" +([.,;:!?])", r"\1", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def assemble(body: str, tags: list[str]) -> str:
    """A post that satisfies every rule: cleaned body, then at most 3 hashtags on the last line."""
    unique = []
    for t in (_clean_tag(t) for t in tags):
        if t and t.lower() not in {u.lower() for u in unique}:
            unique.append(t)
    tag_line = " ".join(f"#{t}" for t in unique[:MAX_HASHTAGS])
    body = clean_body(body)
    room = MAX_CHARS - (len(tag_line) + 2 if tag_line else 0)
    body = _fit(body, room)
    post = f"{body}\n\n{tag_line}" if tag_line else body
    assert not problems(post), problems(post)
    return post
