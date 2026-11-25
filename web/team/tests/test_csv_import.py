"""Tests for CSV import functionality."""

from datetime import date

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from registration.models import Event, EventTeamAssignment, School
from registration.services import NewEvent, SchoolListRow, import_school_list

from team.forms import parse_csv_file, validate_csv_data
from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup_teams() -> list[Team]:
    """Create test teams."""
    teams = []
    for i in range(1, 6):
        team = Team.objects.create(
            team_name=f"Team {i}",
            team_number=i,
            is_active=True,
        )
        teams.append(team)
    return teams


@pytest.fixture
def setup_user() -> User:
    """Create test user."""
    return User.objects.create_user(
        username="testuser",
        email="test@example.com",
        password="testpass123",
    )


class TestCSVParsing:
    """Test CSV parsing and validation."""

    def test_parse_valid_csv(self) -> None:
        """Test parsing a valid CSV file."""
        csv_content = """school_name,contact_email,secondary_email,notes
University One,contact1@example.edu,alt1@example.edu,Test note 1
University Two,contact2@example.edu,,
University Three,contact3@example.edu,alt3@example.edu,Test note 3
"""
        csv_file = SimpleUploadedFile("test.csv", csv_content.encode("utf-8"), content_type="text/csv")

        result = parse_csv_file(csv_file)

        assert len(result["rows"]) == 3
        assert len(result["errors"]) == 0
        assert result["warnings"] == []
        assert result["rows"][0]["school_name"] == "University One"
        assert result["rows"][0]["contact_email"] == "contact1@example.edu"
        assert result["rows"][0]["secondary_email"] == "alt1@example.edu"
        assert result["rows"][0]["notes"] == "Test note 1"

    def test_parse_csv_missing_required_columns(self) -> None:
        """Every required column the header lacks is named."""
        csv_file = SimpleUploadedFile("test.csv", b"notes\nA note\n", content_type="text/csv")

        result = parse_csv_file(csv_file)

        assert result == {
            "rows": [],
            "errors": ["Missing required columns: contact_email, school_name"],
            "warnings": [],
        }

    def test_parse_csv_names_every_bad_row(self) -> None:
        csv_content = """school_name,contact_email
University One,invalid-email
,b@example.edu
"""
        csv_file = SimpleUploadedFile("test.csv", csv_content.encode("utf-8"), content_type="text/csv")

        result = parse_csv_file(csv_file)

        assert result["errors"] == [
            "Row 2: contact_email: Enter a valid email address.",
            "Row 3: school_name: This field is required.",
        ]

    def test_parse_empty_csv(self) -> None:
        """Test parsing empty CSV."""
        csv_content = ""
        csv_file = SimpleUploadedFile("test.csv", csv_content.encode("utf-8"), content_type="text/csv")

        result = parse_csv_file(csv_file)

        assert result == {"rows": [], "errors": ["CSV file is empty or has no headers"], "warnings": []}

    def test_parse_csv_with_only_headers_says_it_has_no_rows(self) -> None:
        result = parse_csv_file(SimpleUploadedFile("test.csv", b"school_name,contact_email\n"))

        assert result == {"rows": [], "errors": ["CSV file contains no data rows"], "warnings": []}

    @pytest.mark.parametrize(
        "content",
        ["school_name,contact_email\nÉcole,a@example.edu\n".encode("cp1252"), "a,b\n".encode("utf-16")],
        ids=["cp1252", "utf-16"],
    )
    def test_a_csv_that_is_not_utf8_asks_for_utf8(self, content: bytes) -> None:
        """A spreadsheet's Windows CSV (cp1252) or its Unicode text (UTF-16) is refused, saying why."""
        result = parse_csv_file(SimpleUploadedFile("test.csv", content))

        assert result == {
            "rows": [],
            "errors": ["File encoding error. Please ensure the file is UTF-8 encoded."],
            "warnings": [],
        }

    @pytest.mark.parametrize(
        ("content", "line"),
        [
            (b'school_name,contact_email\nA,a@example.edu\n"' + b"x" * 140_000, 3),
            (b"school_name,contact_email\nA," + b"y" * 140_000 + b"\n", 2),
            (b'school_name,contact_email\n"' + b"x\n" * 70_000, 2),
            (b'school_name,contact_email\n"Example\nU","' + b"x" * 140_000, 2),
            (b'school_name,contact_email\nA,a@example.edu\n\n\nB,"' + b"x" * 140_000, 5),
            (b'school_name,contact_email\r\nA,a@example.edu\r\n\r\nB,"' + b"x" * 140_000, 4),
            (b'school_name,contact_email\r"Example\rU",a@example.edu\r\rB,"' + b"x" * 140_000, 5),
            (b"school_name," + b"k" * 140_000 + b"\nA,a@example.edu", 1),
        ],
    )
    def test_a_csv_the_reader_gives_up_on_names_the_line_its_bad_row_starts_on(self, content: bytes, line: int) -> None:
        """A row past the csv module's field limit (a quote left open) is refused naming the line it starts on."""
        result = parse_csv_file(SimpleUploadedFile("test.csv", content))

        assert result["errors"] == [
            f"The file isn't a CSV the import can read (line {line}): save it from your spreadsheet as CSV "
            "and upload it again."
        ]

    def test_a_blank_line_is_not_a_row(self) -> None:
        """Row numbers in errors count the header as row 1 and skip blank lines."""
        content = b"school_name,contact_email\n\nA,a@example.edu\n\n\nB,not-an-email\n"

        result = parse_csv_file(SimpleUploadedFile("test.csv", content))

        assert [row["school_name"] for row in result["rows"]] == ["A"]
        assert [error.split(":")[:2] for error in result["errors"]] == [["Row 3", " contact_email"]]

    @pytest.mark.parametrize(
        ("last_cells", "email"),
        [("", None), (",", None), (",last@example.edu,unnamed surplus", "last@example.edu")],
    )
    def test_last_duplicate_contact_email_cell_wins(self, last_cells: str, email: str | None) -> None:
        content = (
            "School,Contact Email,contact_email,contact_email\n"
            f"Example U,alias@example.edu,first@example.edu{last_cells}\n"
        )

        result = parse_csv_file(SimpleUploadedFile("test.csv", content.encode()))

        assert result["warnings"] == [
            "Columns auto-detected: School as school_name",
            "Unknown columns will be ignored: Contact Email",
        ]
        if email is None:
            assert result["rows"] == []
            assert result["errors"] == ["Row 2: contact_email: This field is required."]
        else:
            assert result["errors"] == []
            assert result["rows"] == [
                {"school_name": "Example U", "contact_email": email, "secondary_email": "", "notes": ""}
            ]


class TestCSVHeaderInference:
    """Test auto-detection of CSV column names."""

    def test_infer_email_and_school_columns(self) -> None:
        """Test CSV with non-standard headers like 'Capt. Email' and 'School'."""
        csv_content = """team #,Capt. Email,School
1,captain@example.edu,Springfield High
2,coach@example.edu,Shelbyville Academy
"""
        csv_file = SimpleUploadedFile("test.csv", csv_content.encode("utf-8"), content_type="text/csv")

        result = parse_csv_file(csv_file)

        assert len(result["errors"]) == 0
        assert len(result["rows"]) == 2
        assert result["rows"][0]["school_name"] == "Springfield High"
        assert result["rows"][0]["contact_email"] == "captain@example.edu"
        # Should have an auto-detection warning
        assert any("auto-detected" in w.lower() for w in result["warnings"])

    def test_infer_with_only_two_columns(self) -> None:
        """Test CSV with just email and school columns."""
        csv_content = """Email,School
captain@example.edu,Springfield High
"""
        csv_file = SimpleUploadedFile("test.csv", csv_content.encode("utf-8"), content_type="text/csv")

        result = parse_csv_file(csv_file)

        assert len(result["errors"]) == 0
        assert result["rows"][0]["school_name"] == "Springfield High"
        assert result["rows"][0]["contact_email"] == "captain@example.edu"

    def test_canonical_headers_raise_no_auto_detection_warning(self) -> None:
        csv_content = """school_name,contact_email
Springfield High,captain@example.edu
"""
        csv_file = SimpleUploadedFile("test.csv", csv_content.encode("utf-8"), content_type="text/csv")

        result = parse_csv_file(csv_file)

        assert len(result["errors"]) == 0
        assert not any("auto-detected" in w.lower() for w in result["warnings"])

    def test_two_email_columns_keep_both_emails(self) -> None:
        """Every email column used to map to contact_email, so the last one overwrote the rest."""
        csv_content = """School,Team Captain Email,Coach Email
Example U,captain@example.edu,coach@example.edu
"""
        csv_file = SimpleUploadedFile("test.csv", csv_content.encode("utf-8"), content_type="text/csv")

        result = parse_csv_file(csv_file)

        assert result["errors"] == []
        assert result["rows"][0]["contact_email"] == "captain@example.edu"
        assert result["rows"][0]["secondary_email"] == "coach@example.edu"

    def test_an_email_column_past_the_two_email_slots_is_named_and_ignored(self) -> None:
        csv_file = SimpleUploadedFile(
            "test.csv", b"School,Email,Email 2,Email 3\nExample U,a@example.edu,b@example.edu,c@example.edu\n"
        )

        result = parse_csv_file(csv_file)

        assert result == {
            "rows": [
                {
                    "school_name": "Example U",
                    "contact_email": "a@example.edu",
                    "secondary_email": "b@example.edu",
                    "notes": "",
                }
            ],
            "errors": [],
            "warnings": [
                "Columns auto-detected: Email as contact_email, Email 2 as secondary_email, Email 3 as email_3, "
                "School as school_name",
                "Unknown columns will be ignored: email_3",
            ],
        }

    @pytest.mark.parametrize(
        ("header", "ignored"),
        [
            # Team number columns are skipped: teams are assigned at random
            ("Team No,# 1,Email,School", "#_1, team_no"),
            # With school_name named, the one other column is not taken for it
            ("school_name,Email,Coach", "coach"),
            ("school_name,Email,Coach Name,Region", "coach_name, region"),
        ],
    )
    def test_columns_that_are_not_the_schools_are_named_and_ignored(self, header: str, ignored: str) -> None:
        cells = {"Email": "a@example.edu", "School": "Example U", "school_name": "Example U"}
        row = ",".join(cells.get(name, "7") for name in header.split(","))
        csv_file = SimpleUploadedFile("test.csv", f"{header}\n{row}\n".encode())

        result = parse_csv_file(csv_file)

        assert (result["rows"], result["errors"]) == (
            [{"school_name": "Example U", "contact_email": "a@example.edu", "secondary_email": "", "notes": ""}],
            [],
        )
        assert result["warnings"][1:] == [f"Unknown columns will be ignored: {ignored}"]

    @pytest.mark.parametrize(
        ("header", "row"),
        [
            ("school_name,contact_email,Coach Email", "Example U,captain@example.edu,coach@example.edu"),
            ("school_name,Coach Email,contact_email", "Example U,coach@example.edu,captain@example.edu"),
            # Named in words, the contact column still takes contact_email whichever column comes first
            ("School,Contact Email,Coach Email", "Example U,captain@example.edu,coach@example.edu"),
            ("School,Coach Email,Contact Email", "Example U,coach@example.edu,captain@example.edu"),
        ],
    )
    def test_named_contact_column_wins_and_other_email_is_secondary(self, header: str, row: str) -> None:
        csv_file = SimpleUploadedFile("test.csv", f"{header}\n{row}\n".encode(), content_type="text/csv")

        result = parse_csv_file(csv_file)

        assert result["errors"] == []
        assert result["rows"][0]["contact_email"] == "captain@example.edu"
        assert result["rows"][0]["secondary_email"] == "coach@example.edu"

    @pytest.mark.parametrize(
        ("header", "detected"),
        [
            ("School Name,Contact Email", "School Name as school_name, Contact Email as contact_email"),
            ("school_name, contact_email", " contact_email as contact_email"),
            ("SCHOOL_NAME,Contact_Email", "SCHOOL_NAME as school_name, Contact_Email as contact_email"),
        ],
    )
    def test_headers_naming_the_columns_in_other_case_or_spacing_are_read(self, header: str, detected: str) -> None:
        csv_file = SimpleUploadedFile("test.csv", f"{header}\nExample U,captain@example.edu\n".encode())

        result = parse_csv_file(csv_file)

        assert result == {
            "rows": [
                {
                    "school_name": "Example U",
                    "contact_email": "captain@example.edu",
                    "secondary_email": "",
                    "notes": "",
                }
            ],
            "errors": [],
            "warnings": [f"Columns auto-detected: {detected}"],
        }

    @pytest.mark.parametrize("variant_cell", ["", "other@example.edu"])
    def test_exact_column_keeps_its_name_over_a_header_naming_it_in_other_case(self, variant_cell: str) -> None:
        header = "school_name,contact_email,Contact Email"
        csv_file = SimpleUploadedFile("test.csv", f"{header}\nExample U,captain@example.edu,{variant_cell}\n".encode())

        result = parse_csv_file(csv_file)

        assert result["errors"] == []
        assert result["rows"][0]["contact_email"] == "captain@example.edu"
        assert result["warnings"] == ["Unknown columns will be ignored: Contact Email"]


class TestCSVValidation:
    """Test CSV data validation against database."""

    def test_validate_assigns_random_teams(self, setup_teams: list[Team]) -> None:
        """Test validation assigns teams randomly."""
        rows = [
            {
                "school_name": "University One",
                "contact_email": "contact1@example.edu",
                "secondary_email": "",
                "notes": "",
            },
            {
                "school_name": "University Two",
                "contact_email": "contact2@example.edu",
                "secondary_email": "",
                "notes": "",
            },
        ]

        result = validate_csv_data(rows)

        assert len(result["errors"]) == 0
        assert len(result["teams_to_create"]) == 2
        # Each row should have a team assigned
        for row in result["teams_to_create"]:
            assert "team_number" in row

    def test_validate_uses_teams_one_to_n_and_reports_what_it_replaces(self) -> None:
        """The CSV is the whole school list: teams 1..N regardless of state, existing info replaced."""
        for i in range(1, 5):
            Team.objects.create(team_name=f"Team {i}", team_number=i, is_active=i == 1)
        SchoolInfo.objects.create(
            team=Team.objects.get(team_number=1), school=School.for_name("Old"), contact_email="o@x.edu"
        )

        result = validate_csv_data([_row("University One"), _row("University Two"), _row("University Three")])

        assert result["errors"] == []
        assert sorted(r["team_number"] for r in result["teams_to_create"]) == [1, 2, 3]
        assert "Replaces the 1 existing school record(s)." in result["warnings"]
        assert "Activates team(s) 2, 3." in result["warnings"]

    def test_validate_not_enough_teams(self, setup_teams: list[Team]) -> None:
        result = validate_csv_data([_row(f"University {i}") for i in range(6)])

        assert result == {
            "teams_to_create": [],
            "errors": ["The CSV has 6 schools but only 5 teams exist."],
            "warnings": [],
        }

    @pytest.mark.parametrize(
        ("names", "repeated"),
        [
            (["Example University", "example  university"], "Example University / example  university"),
            (["Uni One", "Uni Two", "uni one", "Uni Two"], "Uni One / uni one, Uni Two / Uni Two"),
        ],
        ids=["case-and-spacing", "every-repeat"],
    )
    def test_validate_names_every_repeated_school(
        self, setup_teams: list[Team], names: list[str], repeated: str
    ) -> None:
        result = validate_csv_data([_row(name) for name in names])

        assert result["errors"] == [f"Each school can appear only once; repeated: {repeated}"]


def _row(school_name: str) -> dict[str, str]:
    return {"school_name": school_name, "contact_email": "contact@example.edu", "secondary_email": "", "notes": ""}


def _list_row(school_name: str, team: Team) -> SchoolListRow:
    return SchoolListRow(team_number=team.team_number, school_name=school_name, contact_email="contact@example.edu")


def _assign(event, team_number, school_name):
    """A school's assignment at an event, as history: not through the school list."""
    return EventTeamAssignment.objects.create(
        event=event, team=Team.objects.get(team_number=team_number), school=School.for_name(school_name)
    )


def _open_event():
    from datetime import date

    from registration.models import Event, Season

    season, _ = Season.objects.get_or_create(year=2026, defaults={"name": "2026", "is_active": True})
    return Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )


class TestCSVImport:
    """Test CSV import application."""

    def test_import_creates_school_info(self, setup_teams: list[Team], setup_user: User) -> None:
        """Test applying CSV import to create new school info."""
        teams_to_create = [
            SchoolListRow(
                team_number=1,
                school_name="University One",
                contact_email="contact1@example.edu",
                secondary_email="alt1@example.edu",
                notes="Test note",
            ),
            SchoolListRow(team_number=2, school_name="University Two", contact_email="contact2@example.edu"),
        ]

        event = _open_event()
        result = import_school_list(event, teams_to_create, "testuser")

        assert result["created"] == 2

        # Verify database
        school_info1 = SchoolInfo.objects.get(team=setup_teams[0])
        assert school_info1.school_name == "University One"
        assert school_info1.school == event.team_assignments.get(team=setup_teams[0]).school
        assert school_info1.contact_email == "contact1@example.edu"
        assert school_info1.secondary_email == "alt1@example.edu"
        assert school_info1.notes == "Test note"
        assert school_info1.updated_by == "testuser"

    def test_apply_replaces_school_info_activates_teams_and_reassigns_the_event(self) -> None:
        from datetime import date

        from registration.models import Event, EventTeamAssignment, Season

        teams = [Team.objects.create(team_name=f"Team {i}", team_number=i, is_active=i == 1) for i in (1, 2, 3)]
        SchoolInfo.objects.create(team=teams[2], school=School.for_name("Old"), contact_email="o@x.edu")
        season = Season.objects.create(name="2026", year=2026, is_active=True)
        event = Event.objects.create(
            season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
        )
        # Last import put this school on team 3; this one moves it to team 1
        EventTeamAssignment.objects.create(event=event, team=teams[2], school=School.for_name("University One"))

        result = import_school_list(
            event,
            [_list_row("University One", teams[0]), _list_row("University Two", teams[1])],
            "gold",
        )

        assert result == {"created": 2, "activated": 1}
        assert sorted(SchoolInfo.objects.values_list("school__name", flat=True)) == ["University One", "University Two"]
        assert sorted(Team.objects.filter(is_active=True).values_list("team_number", flat=True)) == [1, 2]
        assert sorted(
            EventTeamAssignment.objects.filter(event=event).values_list("team__team_number", "school__name")
        ) == [(1, "University One"), (2, "University Two")]


class TestTeamNamesReset:
    """A team's name from the last event (e.g. a school shortname) doesn't carry over to the new school."""

    def test_import_resets_every_team_name(self) -> None:
        renamed = Team.objects.create(team_name="Old Shortname", team_number=1)
        untouched_slot = Team.objects.create(team_name="Old Shortname", team_number=2)

        import_school_list(_open_event(), [_list_row("Example University", renamed)], "gold")

        renamed.refresh_from_db()
        untouched_slot.refresh_from_db()
        assert (renamed.team_name, untouched_slot.team_name) == ("BlueTeam01", "BlueTeam02")

    def test_preview_warns_which_names_are_reset(self) -> None:
        Team.objects.create(team_name="Old Shortname", team_number=1)
        Team.objects.create(team_name="BlueTeam02", team_number=2)
        Team.objects.create(team_name="Another Name", team_number=3)

        result = validate_csv_data([_row("Example University")])

        assert "Resets 2 team name(s) from the last event (Old Shortname, Another Name)." in result["warnings"]


class TestImportOpensEvent:
    @pytest.fixture
    def season(self):
        from registration.models import Season

        return Season.objects.create(name="2026", year=2026, is_active=True)

    def _event(self, season, name, active=False):
        from datetime import date

        from registration.models import Event

        return Event.objects.create(
            season=season, name=name, event_type="invitational", date=date(2026, 10, 3), is_active=active
        )

    def test_apply_opens_the_event(self, setup_teams, season):
        from registration.models import Event, EventTeamAssignment

        self._event(season, "Old")
        new = self._event(season, "New")
        teams = list(Team.objects.order_by("team_number"))

        import_school_list(new, [_list_row("University One", teams[0])], "gold")

        assert list(Event.objects.filter(is_active=True)) == [new]
        assert EventTeamAssignment.objects.get(event=new).school.name == "University One"

    def test_reimport_for_the_open_event_replaces_its_assignments(self, setup_teams, season):
        from registration.models import EventTeamAssignment

        event = self._event(season, "Invitational", active=True)
        teams = list(Team.objects.order_by("team_number"))
        import_school_list(event, [_list_row("University One", teams[0])], "gold")

        import_school_list(event, [_list_row("University One", teams[1])], "gold")

        assignment = EventTeamAssignment.objects.get(event=event)
        assert assignment.team == teams[1]

    def test_preview_marks_returning_and_new_schools(self, setup_teams, season):
        past = self._event(season, "Past")
        _assign(past, 1, "University One")

        result = validate_csv_data([_row("university one"), _row("University Two")])

        marks = {row["school_name"]: row["returning_events"] for row in result["teams_to_create"]}
        assert marks == {"university one": 1, "University Two": None}

    def test_reimporting_for_the_same_event_marks_its_only_school_as_new(self, setup_teams, season):
        event = self._event(season, "Invitational", active=True)
        _assign(event, 1, "University One")

        result = validate_csv_data([_row("University One")], event)

        assert result["teams_to_create"][0]["returning_events"] is None

    def test_a_school_with_a_different_past_event_is_returning_when_reimporting(self, setup_teams, season):
        past = self._event(season, "Past")
        event = self._event(season, "Invitational", active=True)
        _assign(past, 1, "University One")
        _assign(event, 2, "University One")

        result = validate_csv_data([_row("University One")], event)

        assert result["teams_to_create"][0]["returning_events"] == 1

    def test_a_school_without_assignments_is_new(self, setup_teams):

        School.for_name("University One")

        result = validate_csv_data([_row("University One")])

        assert result["teams_to_create"][0]["returning_events"] is None

    def test_preview_warns_when_the_event_already_has_assignments(self, setup_teams, season):
        event = self._event(season, "Invitational", active=True)
        _assign(event, 1, "University One")

        result = validate_csv_data([_row("University Two")], event)

        assert f"Replaces the 1 team assignment(s) for {event}." in result["warnings"]

    def test_preview_has_no_assignment_warning_for_a_new_event(self, setup_teams):
        result = validate_csv_data([_row("University Two")])

        assert not any("team assignment" in w for w in result["warnings"])


def test_a_new_event_from_the_import_form_is_created_only_by_the_import():
    from registration.models import Season

    from team.forms import ImportEventForm

    season = Season.objects.create(name="2026", year=2026, is_active=True)
    form = ImportEventForm(
        {
            "event": "new",
            "new_name": "Qualifier",
            "new_date": "2026-11-07",
            "new_type": "qualifier",
            "current_season": str(season.pk),
        },
        season=season,
    )

    assert form.is_valid()
    assert form.event_to_import == NewEvent("Qualifier", date(2026, 11, 7), "qualifier", season.pk)
    assert not Event.objects.exists()


@pytest.mark.parametrize(
    ("choice", "error"),
    [
        ("²", "That event was closed out or removed: pick another."),
        ("abc", "That event was closed out or removed: pick another."),
        ("-1", "That event was closed out or removed: pick another."),
        ("", "Pick the event this school list is for."),
    ],
)
def test_an_event_choice_that_is_not_an_event_is_an_error_on_the_event(choice, error):
    from registration.models import Season

    from team.forms import ImportEventForm

    season = Season.objects.create(name="2026", year=2026, is_active=True)
    form = ImportEventForm({"event": choice}, season=season)

    assert not form.is_valid()
    assert form.errors["event"] == [error]


@pytest.mark.parametrize("choice", ["", "7"])
def test_with_no_season_active_any_event_choice_but_new_says_so(choice):
    from team.forms import ImportEventForm

    form = ImportEventForm({"event": choice}, season=None)

    assert not form.is_valid()
    assert form.errors["event"] == ["No season is active: import into a new event, with a new season."]


@pytest.mark.parametrize("half", [{"new_season_name": "2027"}, {"new_season_year": "2027"}])
def test_a_new_season_given_by_half_is_refused(half):
    from registration.models import Season

    from team.forms import ImportEventForm

    season = Season.objects.create(name="2026", year=2026, is_active=True)
    new = {"event": "new", "new_name": "Qualifier", "new_date": "2026-11-07", "new_type": "qualifier"}
    form = ImportEventForm(new | {"current_season": season.pk} | half, season=season)

    assert not form.is_valid()
    assert form.errors == {"__all__": ["Give the new season a name and a year."]}


def test_a_new_season_for_an_existing_event_is_refused():
    from team.forms import ImportEventForm

    event = _open_event()
    form = ImportEventForm({"event": str(event.pk), "new_season_name": "2027"}, season=event.season)

    assert not form.is_valid()
    assert form.errors == {
        "__all__": ["A new season needs a new event: choose New event, or leave the new season empty."]
    }


def test_an_upload_that_is_not_named_csv_is_refused():
    from team.forms import CSVUploadForm

    form = CSVUploadForm(files={"csv_file": SimpleUploadedFile("schools.txt", b"school_name,contact_email\n")})

    assert form.errors == {"csv_file": ["File must be a CSV file (.csv)"]}


@pytest.mark.parametrize(
    ("size", "errors"),
    [(10 * 1024 * 1024, {}), (10 * 1024 * 1024 + 1, {"csv_file": ["File size must be less than 10MB"]})],
)
def test_an_upload_may_be_10_mib_and_no_more(size, errors):
    from team.forms import CSVUploadForm

    form = CSVUploadForm(files={"csv_file": SimpleUploadedFile("schools.csv", b"x" * size)})

    assert form.errors == errors


def test_the_import_form_labels_its_event_choice_and_new_event_fields():
    """With no event open the Add mode names "the open event"; the new event's date is a date picker."""
    from team.forms import ImportEventForm

    form = ImportEventForm(season=None)

    assert form.fields["event"].choices[0] == ("", "Choose an event")
    assert dict(form.fields["mode"].choices)["add"] == "Add schools to the open event"
    assert [(form[name].label, form[name].help_text) for name in ("new_name", "new_date", "new_type")] == [
        ("New event name", "Only for a new event."),
        ("New event date", ""),
        ("New event type", ""),
    ]
    assert form.fields["new_date"].widget.input_type == "date"
