"""A finding's points and outcome list follow the CCDC table: root access beats user access, the rest add up."""

from decimal import Decimal
from itertools import product

from scoring.models import RedTeamScore

FIELDS = (
    "root_access",
    "user_access",
    "privilege_escalation",
    "credentials_recovered",
    "sensitive_files_recovered",
    "credit_cards_recovered",
    "pii_recovered",
    "encrypted_db_recovered",
    "db_decrypted",
)

# The scoring guidelines' table, written out independently of the model's
ADDITIVE = {
    "privilege_escalation": (100, "Privilege Escalation"),
    "credentials_recovered": (50, "Credentials"),
    "sensitive_files_recovered": (25, "Sensitive Files"),
    "credit_cards_recovered": (50, "Credit Cards"),
    "pii_recovered": (200, "PII"),
    "encrypted_db_recovered": (25, "Encrypted DB"),
    "db_decrypted": (25, "DB Decrypted"),
}


def expected(flags: dict[str, bool]) -> tuple[Decimal, list[str]]:
    total, shown = 0, []
    if flags["root_access"]:
        total, shown = 100, ["Root Access (-100)"]
    elif flags["user_access"]:
        total, shown = 25, ["User Access (-25)"]
    for field, (points, label) in ADDITIVE.items():
        if flags[field]:
            total += points
            shown.append(f"{label} (-{points})")
    return Decimal(total), shown


def test_points_and_outcomes_for_every_combination() -> None:
    for values in product((False, True), repeat=len(FIELDS)):
        flags = dict(zip(FIELDS, values, strict=True))
        finding = RedTeamScore(**flags)
        assert (finding.calculate_points(), finding.outcomes_display) == expected(flags), flags


def test_the_outcome_table_names_every_outcome_field_once() -> None:
    assert [outcome.field for outcome in RedTeamScore.OUTCOMES] == list(FIELDS)
