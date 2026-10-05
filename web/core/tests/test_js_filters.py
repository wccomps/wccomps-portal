from core.templatetags.js_filters import jskey


def test_jskey_prefixes_numeric_id() -> None:
    assert jskey(3) == "k3"
    assert jskey("42") == "k42"


def test_jskey_sanitizes_ticket_number() -> None:
    # Hyphens are not valid in a JS property path, so the binding would break without this.
    assert jskey("T001-001") == "kT001_001"


def test_jskey_matches_js_helper_rule() -> None:
    # Mirror of jsKey() in static/js/utils.js: 'k' + every non-word char -> '_'.
    assert jskey("a b.c-d") == "ka_b_c_d"
