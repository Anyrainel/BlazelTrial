from scenarios.checks import budget_problems


def test_budget_evidence_requires_real_usage_and_counting():
    good = dict(input_tokens=7999, within_budget=True, preflight_method="server")
    assert not budget_problems([good], 8000)
    assert budget_problems([], 8000)
    for change in (
        dict(input_tokens=8000),
        dict(input_tokens=0),
        dict(input_tokens=None),
        dict(preflight_method="estimate"),
        dict(within_budget=False),
    ):
        assert budget_problems([good | change], 8000)


def test_preflight_refusal_is_not_a_sent_call():
    refusal = dict(input_tokens=None, error="preflight_over_budget")
    good = dict(input_tokens=50, within_budget=True, preflight_method="tokenizer")
    assert not budget_problems([refusal, good], 8000)
    assert budget_problems([refusal], 8000)
