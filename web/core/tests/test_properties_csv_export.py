"""CSV the portal writes is opened in spreadsheets, so user text in it must not run as a formula (CSV injection).

Every exporter writes through core.csv_export; test_property_coverage fails on a CSV writer that doesn't.
"""

import csv
import io
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from hypothesis import example, given
from hypothesis import strategies as st
from registration.models import School

from core.authentik_utils import credentials_csv, generate_blueteam_password
from core.csv_export import FORMULA_STARTS, csv_writer
from core.tests.strategies import PAYLOADS, request_text, rolled_back
from team.models import SchoolInfo, Team

TEXT = st.one_of(request_text(), st.sampled_from(PAYLOADS), st.sampled_from(FORMULA_STARTS).map(lambda c: c + "1+1"))
NUMBERS = st.one_of(st.integers(), st.decimals(allow_nan=False, allow_infinity=False, places=2))


def _round_trip(row: list[object]) -> list[str]:
    out = io.StringIO()
    csv_writer(out).writerow(row)
    return next(csv.reader(io.StringIO(out.getvalue(), newline="")))


@given(cells=st.lists(st.one_of(TEXT, NUMBERS), min_size=1, max_size=6))
@example(cells=['=HYPERLINK("http://x","y")', -5, Decimal("-1.50"), "\t=1", "\r=1", "@SUM(1)", "+1", "-2+3"])
def test_an_exported_text_cell_never_starts_a_formula_and_reads_back_with_only_the_prefix(cells: list[object]) -> None:
    for written, read in zip(cells, _round_trip(cells), strict=True):
        if isinstance(written, str):
            if written.startswith(FORMULA_STARTS):
                assert read == "'" + written
            else:
                assert read == written
                assert not read.startswith(FORMULA_STARTS)
        else:
            assert read == str(written)  # numbers stay numbers, unquoted


@pytest.mark.django_db
# Team.save runs full_clean: a blank, too long or NUL name can't be stored, so it can't be exported either
@given(team_name=request_text(100).filter(lambda s: s.strip() and len(s) <= 100 and "\x00" not in s))
@example(team_name='=HYPERLINK("http://x","y")')
def test_a_team_name_in_the_school_list_export_is_not_a_formula(team_name: str, gold_team_user: User) -> None:
    client = Client()
    client.force_login(gold_team_user)
    with rolled_back():
        team = Team.objects.create(team_number=1, team_name=team_name)
        SchoolInfo.objects.create(team=team, school=School.for_name("School"), contact_email="c@example.edu")
        response = client.get(reverse("school_info_export"))
        rows = list(csv.reader(io.StringIO(response.content.decode(), newline="")))
    exported = rows[1][1]
    assert exported == ("'" + team_name if team_name.startswith(FORMULA_STARTS) else team_name)


@given(
    accounts=st.lists(
        st.tuples(st.from_regex(r"team[0-9]{2}", fullmatch=True), st.sampled_from(("!", "@", "#", "+", "=", "-", "a"))),
        max_size=5,
    ).map(lambda rows: [(user, start + "123-Correct-Horse") for user, start in rows])
)
@example(accounts=[("team01", "@123-Apple-Banana"), ("team02", "+456-Pear-Plum")])
def test_new_passwords_read_back_from_their_csv_exactly(accounts: list[tuple[str, str]]) -> None:
    """The credentials CSV holds generated passwords, not user text: a quote added in front would hand out a
    wrong password."""
    rows = list(csv.reader(io.StringIO(credentials_csv(accounts), newline="")))
    assert rows == [["Username", "Password"], *[[user, password] for user, password in accounts]]


def test_every_generated_password_reads_back_from_its_csv_exactly() -> None:
    passwords = [generate_blueteam_password() for _ in range(500)]
    rows = list(csv.reader(io.StringIO(credentials_csv(("team01", p) for p in passwords), newline="")))
    assert [row[1] for row in rows[1:]] == passwords
