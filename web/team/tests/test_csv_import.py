"""Tests for CSV import functionality."""

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from registration.models import EventTeamAssignment, School
from registration.services import import_school_list

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
        assert result["rows"][0]["school_name"] == "University One"
        assert result["rows"][0]["contact_email"] == "contact1@example.edu"
        assert result["rows"][0]["secondary_email"] == "alt1@example.edu"
        assert result["rows"][0]["notes"] == "Test note 1"

    def test_parse_csv_missing_required_columns(self) -> None:
        """Test parsing CSV with missing required columns."""
        csv_content = """school_name
University One
"""
        csv_file = SimpleUploadedFile("test.csv", csv_content.encode("utf-8"), content_type="text/csv")

        result = parse_csv_file(csv_file)

        assert len(result["errors"]) > 0
        assert "contact_email" in result["errors"][0]

    def test_parse_csv_invalid_email(self) -> None:
        """Test parsing CSV with invalid email."""
        csv_content = """school_name,contact_email
University One,invalid-email
"""
        csv_file = SimpleUploadedFile("test.csv", csv_content.encode("utf-8"), content_type="text/csv")

        result = parse_csv_file(csv_file)

        assert len(result["errors"]) > 0
        assert "not a valid email" in result["errors"][0]

    def test_parse_empty_csv(self) -> None:
        """Test parsing empty CSV."""
        csv_content = ""
        csv_file = SimpleUploadedFile("test.csv", csv_content.encode("utf-8"), content_type="text/csv")

        result = parse_csv_file(csv_file)

        assert len(result["errors"]) > 0


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

    def test_canonical_headers_no_inference(self) -> None:
        """Test that canonical headers bypass inference."""
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

    @pytest.mark.parametrize(
        ("header", "row"),
        [
            ("school_name,contact_email,Coach Email", "Example U,captain@example.edu,coach@example.edu"),
            ("school_name,Coach Email,contact_email", "Example U,coach@example.edu,captain@example.edu"),
        ],
    )
    def test_named_contact_column_wins_and_other_email_is_secondary(self, header: str, row: str) -> None:
        csv_file = SimpleUploadedFile("test.csv", f"{header}\n{row}\n".encode(), content_type="text/csv")

        result = parse_csv_file(csv_file)

        assert result["errors"] == []
        assert result["rows"][0]["contact_email"] == "captain@example.edu"
        assert result["rows"][0]["secondary_email"] == "coach@example.edu"


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
            assert "_team" in row
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

        assert result["errors"] == ["The CSV has 6 schools but only 5 teams exist."]

    def test_validate_rejects_a_repeated_school(self, setup_teams: list[Team]) -> None:
        result = validate_csv_data([_row("University One"), _row("University One")])

        assert result["errors"] == ["Each school can appear only once; repeated: University One / University One"]


def _row(school_name: str) -> dict[str, str]:
    return {"school_name": school_name, "contact_email": "contact@example.edu", "secondary_email": "", "notes": ""}


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
            {
                "_team": setup_teams[0],
                "team_number": 1,
                "school_name": "University One",
                "contact_email": "contact1@example.edu",
                "secondary_email": "alt1@example.edu",
                "notes": "Test note",
            },
            {
                "_team": setup_teams[1],
                "team_number": 2,
                "school_name": "University Two",
                "contact_email": "contact2@example.edu",
                "secondary_email": "",
                "notes": "",
            },
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
            [{**_row("University One"), "_team": teams[0]}, {**_row("University Two"), "_team": teams[1]}],
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

        import_school_list(_open_event(), [{**_row("Example University"), "_team": renamed}], "gold")

        renamed.refresh_from_db()
        untouched_slot.refresh_from_db()
        assert (renamed.team_name, untouched_slot.team_name) == ("BlueTeam01", "BlueTeam02")

    def test_preview_warns_which_names_are_reset(self) -> None:
        Team.objects.create(team_name="Old Shortname", team_number=1)
        Team.objects.create(team_name="BlueTeam02", team_number=2)

        result = validate_csv_data([_row("Example University")])

        assert "Resets 1 team name(s) from the last event (Old Shortname)." in result["warnings"]


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

        import_school_list(new, [{**_row("University One"), "_team": teams[0]}], "gold")

        assert list(Event.objects.filter(is_active=True)) == [new]
        assert EventTeamAssignment.objects.get(event=new).school.name == "University One"

    def test_reimport_for_the_open_event_replaces_its_assignments(self, setup_teams, season):
        from registration.models import EventTeamAssignment

        event = self._event(season, "Invitational", active=True)
        teams = list(Team.objects.order_by("team_number"))
        import_school_list(event, [{**_row("University One"), "_team": teams[0]}], "gold")

        import_school_list(event, [{**_row("University One"), "_team": teams[1]}], "gold")

        assignment = EventTeamAssignment.objects.get(event=event)
        assert assignment.team == teams[1]

    def test_validate_rejects_names_that_differ_only_by_case_or_spacing(self, setup_teams):
        result = validate_csv_data([_row("Example University"), _row("example  university")])

        assert result["errors"] and "repeated" in result["errors"][0]

    def test_preview_marks_returning_and_new_schools(self, setup_teams, season):
        past = self._event(season, "Past")
        _assign(past, 1, "University One")

        result = validate_csv_data([_row("university one"), _row("University Two")])

        marks = {row["school_name"]: row["returning_events"] for row in result["teams_to_create"]}
        assert marks == {"university one": 1, "University Two": None}

    def test_reimporting_for_the_same_event_marks_its_only_school_as_new(self, setup_teams, season):
        event = self._event(season, "Invitational", active=True)
        _assign(event, 1, "University One")

        result = validate_csv_data([_row("University One")], event.pk)

        assert result["teams_to_create"][0]["returning_events"] is None

    def test_a_school_with_a_different_past_event_is_returning_when_reimporting(self, setup_teams, season):
        past = self._event(season, "Past")
        event = self._event(season, "Invitational", active=True)
        _assign(past, 1, "University One")
        _assign(event, 2, "University One")

        result = validate_csv_data([_row("University One")], event.pk)

        assert result["teams_to_create"][0]["returning_events"] == 1

    def test_a_school_without_assignments_is_new(self, setup_teams):

        School.for_name("University One")

        result = validate_csv_data([_row("University One")])

        assert result["teams_to_create"][0]["returning_events"] is None

    def test_preview_warns_when_the_event_already_has_assignments(self, setup_teams, season):
        event = self._event(season, "Invitational", active=True)
        _assign(event, 1, "University One")

        result = validate_csv_data([_row("University Two")], event.pk)

        assert f"Replaces the 1 team assignment(s) for {event}." in result["warnings"]

    def test_preview_has_no_assignment_warning_for_a_new_event(self, setup_teams):
        result = validate_csv_data([_row("University Two")])

        assert not any("team assignment" in w for w in result["warnings"])


def test_a_new_event_from_the_import_form_is_unsaved_and_closed_to_registration():
    from registration.models import Season

    from team.forms import ImportEventForm

    season = Season.objects.create(name="2026", year=2026, is_active=True)
    form = ImportEventForm(
        {"event": "new", "new_name": "Qualifier", "new_date": "2026-11-07", "new_type": "qualifier"}, season=season
    )

    assert form.is_valid()
    assert form.event_to_import._state.adding
    assert form.event_to_import.registration_open is False


@pytest.mark.parametrize("choice", ["²", "abc", "-1", ""])
def test_an_event_choice_that_is_not_a_number_is_a_form_error(choice):
    from registration.models import Season

    from team.forms import ImportEventForm

    season = Season.objects.create(name="2026", year=2026, is_active=True)
    form = ImportEventForm({"event": choice}, season=season)

    assert not form.is_valid()
    assert form.non_field_errors() == ["Pick the event this school list is for."]
