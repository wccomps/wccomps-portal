"""The view property harness's own checks: its oracle refuses what it should, and its tables stay in step with the
code they stand for."""

from django.http import HttpResponse

from core.tests.views_harness import Answer, Drawn, ViewCase, check_answer


def _answer(status: int) -> Answer:
    return Answer(HttpResponse(status=status), b"", [], [])


def test_a_400_to_a_refused_role_passes_only_for_a_nul() -> None:
    """A view that validates its input before its permission check would answer a refused role 400."""
    case = ViewCase("ticket_list", frozenset({"admin"}))
    plain = Drawn("red", "GET", "/tickets/", {"search": ["x"]}, {}, None, {}, None, ["x"])
    try:
        check_answer(case, plain, _answer(400))
    except AssertionError:
        pass
    else:
        raise AssertionError("a 400 without a NUL passed as a refusal")
    with_nul = Drawn("red", "GET", "/tickets/", {"search": ["\x00"]}, {}, None, {}, None, ["\x00"])
    check_answer(case, with_nul, _answer(400))
