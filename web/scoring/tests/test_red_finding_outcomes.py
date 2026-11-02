"""A finding's points and outcome list follow the CCDC table: its one access level, then the rest add up."""

from decimal import Decimal
from itertools import product

from scoring.models import RedTeamScore

LEVELS = {"none": (0, []), "user": (25, ["User Access (-25)"]), "root": (100, ["Root Access (-100)"])}

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


def expected(level: str, flags: dict[str, bool]) -> tuple[Decimal, list[str]]:
    total, shown = LEVELS[level][0], list(LEVELS[level][1])
    for field, (points, label) in ADDITIVE.items():
        if flags[field]:
            total += points
            shown.append(f"{label} (-{points})")
    return Decimal(total), shown


def test_points_and_outcomes_for_every_combination() -> None:
    for level in LEVELS:
        for values in product((False, True), repeat=len(ADDITIVE)):
            flags = dict(zip(ADDITIVE, values, strict=True))
            finding = RedTeamScore(access_level=level, **flags)
            assert (finding.calculate_points(), finding.outcomes_display) == expected(level, flags), (level, flags)


def test_the_outcome_table_names_every_outcome_field_once() -> None:
    assert [outcome.field for outcome in RedTeamScore.OUTCOMES] == list(ADDITIVE)
    assert set(RedTeamScore.ACCESS_OUTCOMES) == {"user", "root"}
