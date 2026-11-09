from agentcli.reasoning import effort_levels, resolve_effort, resolve_thinking, thinking_levels


def test_exaone_effort_mapping_rounds_up():
    got = {lvl: (resolve_effort("exaone", lvl).applied, resolve_effort("exaone", lvl).clamped)
           for lvl in ("minimal", "low", "medium", "high", "xhigh", "max")}
    assert got == {"minimal": ("low", True), "low": ("low", False),
                   "medium": ("high", True), "high": ("high", False),
                   "xhigh": ("max", True), "max": ("max", False)}
    assert effort_levels("exaone") == frozenset({"low", "high", "max"})


def test_exaone_thinking_is_reported_unsupported_not_raised():
    res = resolve_thinking("exaone", "detailed")
    assert res.supported is False and res.applied == ""
    assert thinking_levels("exaone") == frozenset()
