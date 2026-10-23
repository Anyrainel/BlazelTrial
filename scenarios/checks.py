"""Independent checks for evidence produced by real scenario runs."""


def budget_problems(rows: list[dict], limit: int) -> list[str]:
    """Fail closed on missing usage, estimate fallback, or inconsistent budget flags."""
    sent = [r for r in rows if r.get("input_tokens") is not None]
    problems = []
    if not sent:
        problems.append("no provider-reported calls")
    for row in rows:
        if row.get("input_tokens") is None:
            if row.get("error") != "preflight_over_budget":
                problems.append("unaccounted request without usage")
            continue
        tokens = row["input_tokens"]
        if not isinstance(tokens, int) or tokens <= 0 or tokens >= limit:
            problems.append("invalid or over-budget provider usage")
        if row.get("within_budget") is not True:
            problems.append("provider budget flag failed")
        if row.get("preflight_method") not in ("server", "tokenizer"):
            problems.append("live inference without API token count")
    return problems
