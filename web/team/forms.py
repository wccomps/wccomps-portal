import csv
import io
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TypedDict, cast

from django import forms
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db.models import Count, Q
from registration.forms import NewEventFields
from registration.models import Event, School, Season, normalize_school_name
from registration.services import (
    ADD_NEEDS_OPEN_EVENT,
    NewEvent,
    NewSeason,
    SchoolListRow,
    add_problem,
    add_to_school_list,
    free_teams,
    held_schools,
    import_problem,
    import_school_list,
)

from core.csv_export import unquote_cell
from core.forms import SchoolInfoEditForm
from team.models import SchoolInfo, Team, default_team_name


def _infer_header_mapping(fieldnames: list[str]) -> dict[str, str] | None:
    """Infer canonical column names by inspecting header text.

    Maps other email columns (contain 'email'), in order, to whichever of contact_email and
    secondary_email is still free; any beyond those are ignored. Assigns the remaining
    unmapped column as school_name. Returns None if headers already canonical.
    """
    canonical = {"school_name", "contact_email", "secondary_email", "notes"}
    normalized = {f: f.strip().lower().replace(" ", "_") for f in fieldnames}

    if set(normalized.values()) <= canonical:
        return None

    mapping: dict[str, str] = {}
    unmapped: list[str] = []
    skipped: list[str] = []
    # Email slots not already taken by a column named exactly that
    email_columns = iter(c for c in ("contact_email", "secondary_email") if c not in normalized.values())
    for raw in fieldnames:
        norm = raw.strip().lower()
        if norm.replace(" ", "_") in canonical:
            mapping[raw] = norm.replace(" ", "_")
        elif "email" in norm:
            mapping[raw] = next(email_columns, norm.replace(" ", "_"))
        elif "team" in norm or norm.replace(" ", "").replace("#", "").isdigit():
            # Skip team number columns — teams are assigned randomly
            skipped.append(raw)
        else:
            unmapped.append(raw)

    # Single remaining unmapped column is the school name
    if len(unmapped) == 1 and "school_name" not in mapping.values():
        mapping[unmapped[0]] = "school_name"
    else:
        for raw in unmapped:
            mapping[raw] = raw.strip().lower().replace(" ", "_")

    # Include skipped columns so they appear in the warning but get ignored
    for raw in skipped:
        mapping[raw] = raw.strip().lower().replace(" ", "_")

    return mapping


class CSVRowData(TypedDict, total=False):
    school_name: str
    contact_email: str
    secondary_email: str
    notes: str
    team_number: int
    returning_events: int | None


class CSVParseResult(TypedDict):
    rows: list[CSVRowData]
    errors: list[str]
    warnings: list[str]


class CSVValidationResult(TypedDict):
    teams_to_create: list[CSVRowData]
    errors: list[str]
    warnings: list[str]


class CSVUploadForm(forms.Form):
    csv_file = forms.FileField(
        label="CSV File",
        help_text=(
            "Upload a CSV file with team school information. "
            "Required columns: school_name, contact_email. "
            "Optional: secondary_email, notes. "
            "Column names are auto-detected from headers."
        ),
    )

    def clean_csv_file(self) -> UploadedFile[bytes]:
        csv_file = cast(UploadedFile[bytes], self.cleaned_data["csv_file"])

        if not csv_file.name or not csv_file.name.endswith(".csv"):
            raise ValidationError("File must be a CSV file (.csv)")

        if csv_file.size and csv_file.size > 10 * 1024 * 1024:
            raise ValidationError("File size must be less than 10MB")

        return csv_file


class ImportEventForm(NewEventFields):
    """The event a school list is for: an unfinalized event of the active season, or a new one, in a new season when
    none is active or at a new school year; and the import mode's key (IMPORT_MODES), Replace when not given."""

    event = forms.CharField(required=False)
    mode = forms.CharField(required=False)
    new_season_name = forms.CharField(max_length=100, required=False)
    new_season_year = forms.IntegerField(
        min_value=2000, max_value=2100, required=False, error_messages={"invalid": "Give the season a valid year."}
    )

    # After validation: the chosen event, or the new one the import creates; and the mode's key
    event_to_import: Event | NewEvent
    import_mode: str

    def __init__(self, *args: object, season: Season | None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.season = season

    def wants_new_season(self) -> bool:
        return bool(self.cleaned_data.get("new_season_name") or self.cleaned_data.get("new_season_year"))

    def new_season(self) -> NewSeason | None:
        for name in ("new_season_name", "new_season_year"):
            if name in self.errors:
                raise ValidationError(self.errors[name][0])
        if not self.wants_new_season():
            return None
        data = self.cleaned_data
        if not (data.get("new_season_name") and data.get("new_season_year")):
            raise ValidationError("Give the new season a name and a year.")
        return NewSeason(name=data["new_season_name"], year=data["new_season_year"])

    def clean(self) -> dict[str, object]:
        data = super().clean() or {}
        choice = str(data.get("event") or "")
        if choice == "new":
            self.event_to_import = self.new_event()
        elif self.wants_new_season():
            raise ValidationError("A new season needs a new event: choose New event, or leave the new season empty.")
        elif self.season is None:
            raise ValidationError("No season is active: import into a new event, with a new season.")
        elif not choice.isdecimal():
            raise ValidationError("Pick the event this school list is for.")
        else:
            chosen = self.season.events.filter(pk=int(choice), is_finalized=False).first()
            if chosen is None:
                raise ValidationError("That event was closed out or removed.")
            self.event_to_import = chosen
        self.import_mode = str(data.get("mode") or "replace")
        if self.import_mode not in IMPORT_MODES:
            raise ValidationError("Pick Replace or Add.")
        return data


class ImportConfirmForm(forms.Form):
    """The confirm of an import preview: the token its preview was stored under in the session."""

    preview = forms.CharField(max_length=64)


def parse_csv_file(csv_file: UploadedFile[bytes]) -> CSVParseResult:
    """Parse CSV file and validate contents."""
    rows: list[CSVRowData] = []
    errors: list[str] = []
    warnings: list[str] = []

    try:
        # utf-8-sig: spreadsheet programs start a UTF-8 CSV with a byte order mark
        content = csv_file.read().decode("utf-8-sig")
        csv_file.seek(0)

        # newline="": the csv module reads line ends itself, so CR alone, LF and CRLF all end a row
        reader = csv.DictReader(io.StringIO(content, newline=""))

        required_headers = {"school_name", "contact_email"}
        optional_headers = {"secondary_email", "notes"}
        all_headers = required_headers | optional_headers

        if not reader.fieldnames:
            errors.append("CSV file is empty or has no headers")
            return {"rows": rows, "errors": errors, "warnings": warnings}

        header_mapping = _infer_header_mapping(list(reader.fieldnames))
        if header_mapping:
            mapped_names = set(header_mapping.values())
            warnings.append(
                "Columns auto-detected: " + ", ".join(f"{raw} as {canon}" for raw, canon in header_mapping.items())
            )
        else:
            mapped_names = set(reader.fieldnames)

        headers = mapped_names

        missing_headers = required_headers - headers
        if missing_headers:
            errors.append(f"Missing required columns: {', '.join(sorted(missing_headers))}")
            return {"rows": rows, "errors": errors, "warnings": warnings}

        unknown_headers = headers - all_headers
        if unknown_headers:
            warnings.append(f"Unknown columns will be ignored: {', '.join(sorted(unknown_headers))}")

        for row_num, raw_row in enumerate(reader, start=2):  # Start at 2 (header is row 1)
            row = {header_mapping.get(k, k): v for k, v in raw_row.items()} if header_mapping else raw_row
            # A row is checked as the school info form checks one school: lengths, emails, no NUL characters.
            # A cell missing from a short row is None.
            # A cell may be the school info export's, so the formula guard's quote comes off first
            form = SchoolInfoEditForm({name: unquote_cell(row.get(name) or "") for name in all_headers})
            if form.is_valid():
                rows.append(
                    {
                        "school_name": form.cleaned_data["school_name"],
                        "contact_email": form.cleaned_data["contact_email"],
                        "secondary_email": form.cleaned_data["secondary_email"],
                        "notes": form.cleaned_data["notes"],
                    }
                )
            else:
                errors += [
                    f"Row {row_num}: {field}: {error}" for field, messages in form.errors.items() for error in messages
                ]

        if not rows and not errors:
            errors.append("CSV file contains no data rows")

    except UnicodeDecodeError:
        errors.append("File encoding error. Please ensure the file is UTF-8 encoded.")
    except csv.Error:
        errors.append(
            f"The file isn't a CSV the import can read (line {reader.line_num}): save it from your spreadsheet as CSV "
            "and upload it again."
        )

    return {"rows": rows, "errors": errors, "warnings": warnings}


def _repeated_schools(rows: list[CSVRowData]) -> list[str]:
    """Schools named by more than one row; schools are matched by normalized name, and hold one team per event."""
    by_key: dict[str, list[str]] = {}
    for row in rows:
        by_key.setdefault(normalize_school_name(row["school_name"]), []).append(row["school_name"])
    repeated = [" / ".join(names) for names in by_key.values() if len(names) > 1]
    return [f"Each school can appear only once; repeated: {', '.join(repeated)}"] if repeated else []


def _assign(rows: list[CSVRowData], teams: list[Team], event_id: int | None) -> list[CSVRowData]:
    """Give the rows the teams in random order and mark returning schools.

    Other events only: the event being imported (a re-import) doesn't make its schools returning."""
    random.shuffle(teams)
    for team, row in zip(teams, rows, strict=True):
        row["team_number"] = team.team_number
    past = School.objects.filter(name_key__in=[normalize_school_name(r["school_name"]) for r in rows]).annotate(
        past=Count("assignments", filter=~Q(assignments__event_id=event_id) if event_id is not None else Q())
    )
    known = {school.name_key: school.past for school in past if school.past}
    for row in rows:
        row["returning_events"] = known.get(normalize_school_name(row["school_name"]))
    return rows


def _team_warnings(teams: list[Team], renamed_teams: Iterable[Team]) -> list[str]:
    """The import activates the rows' teams and resets the names of renamed_teams."""
    warnings = []
    if inactive := [t.team_number for t in teams if not t.is_active]:
        warnings.append(f"Activates team(s) {', '.join(map(str, inactive))}.")
    if renamed := [t.team_name for t in renamed_teams if t.team_name != default_team_name(t.team_number)]:
        warnings.append(f"Resets {len(renamed)} team name(s) from the last event ({', '.join(renamed)}).")
    return warnings


def validate_csv_data(rows: list[CSVRowData], event: Event | NewEvent | None = None) -> CSVValidationResult:
    """Assign the rows to teams 1..N in random order; the import replaces all existing school info.

    The event is the one imported into; None, or a NewEvent, is a new one.
    """
    saved = event if isinstance(event, Event) else None
    event_id = saved.pk if saved is not None else None
    errors = _repeated_schools(rows)
    warnings: list[str] = []

    num_rows = len(rows)
    teams = list(Team.objects.order_by("team_number")[:num_rows])

    if len(teams) < num_rows:
        errors.append(f"The CSV has {num_rows} schools but only {len(teams)} teams exist.")
    if errors:
        return {"teams_to_create": [], "errors": errors, "warnings": warnings}

    existing = SchoolInfo.objects.count()
    if existing:
        warnings.append(f"Replaces the {existing} existing school record(s).")
    # Every team name is reset, not only the rows' teams
    warnings.extend(_team_warnings(teams, Team.objects.all()))

    if saved is not None:
        replaced = saved.team_assignments.count()
        if replaced:
            warnings.append(f"Replaces the {replaced} team assignment(s) for {saved}.")

    return {"teams_to_create": _assign(rows, teams, event_id), "errors": errors, "warnings": warnings}


def validate_csv_additions(rows: list[CSVRowData], event: Event | NewEvent) -> CSVValidationResult:
    """Give the rows' schools free teams, the lowest numbers in random order, at the open event.

    Schools already on the list (or at the event) keep their teams and are skipped, so nobody moves."""
    if isinstance(event, NewEvent):
        return {"teams_to_create": [], "errors": [ADD_NEEDS_OPEN_EVENT], "warnings": []}
    errors = _repeated_schools(rows)
    warnings: list[str] = []

    keys = [normalize_school_name(row["school_name"]) for row in rows]
    on_list = held_schools(keys, event)
    new_rows = [row for row, key in zip(rows, keys, strict=True) if key not in on_list]
    if skipped := [row["school_name"] for row, key in zip(rows, keys, strict=True) if key in on_list]:
        warnings.append(f"Already on the list, skipped: {', '.join(skipped)}")
    if not new_rows:
        errors.append("Every school in the file is already on the list.")

    free = free_teams(event).order_by("team_number")
    teams = list(free[: len(new_rows)])
    if len(teams) < len(new_rows):
        errors.append(
            f"Adding {len(new_rows)} school(s) needs {len(new_rows)} free team(s), but only {free.count()} are free."
        )
    if errors:
        return {"teams_to_create": [], "errors": errors, "warnings": warnings}

    warnings.extend(_team_warnings(teams, teams))
    return {"teams_to_create": _assign(new_rows, teams, event.pk), "errors": errors, "warnings": warnings}


@dataclass(frozen=True)
class ImportMode:
    """One way to import a school list. Its services re-check the event and the teams under the school list lock."""

    label: str  # What the page calls it; {event} is the event imported into
    problem: Callable[[Event | NewEvent], str]  # Why the event can't take the import now, or ''
    validate: Callable[[list[CSVRowData], Event | NewEvent], CSVValidationResult]
    preview: Callable[[Event | NewEvent, list[CSVRowData]], str]  # A sentence on what the import keeps, or ''
    apply: Callable[[Event | NewEvent, list[SchoolListRow], str], dict[str, int]]
    result: Callable[[Event | NewEvent, dict[str, int]], list[str]]  # The lines saying what it did


IMPORT_MODES = {
    "replace": ImportMode(
        label="Replace the school list",
        problem=import_problem,
        validate=validate_csv_data,
        preview=lambda event, rows: "",
        apply=import_school_list,
        result=lambda event, counts: [
            "Import completed successfully!",
            f"Created: {counts['created']} school info record(s)",
        ],
    ),
    "add": ImportMode(
        label="Add schools to {event}",
        problem=add_problem,
        validate=validate_csv_additions,
        preview=lambda event, rows: (
            f"Adds {len(rows)} school(s) to {event}; the {SchoolInfo.objects.count()} school(s) already on the list "
            "keep their team numbers."
        ),
        apply=add_to_school_list,
        result=lambda event, counts: [f"Added {counts['created']} school(s) to {event}."],
    ),
}
