import csv
import io
import random
from collections.abc import Iterable
from typing import NotRequired, TypedDict, cast

from django import forms
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.core.validators import validate_email
from django.db.models import Count, Q
from registration.models import Event, School, Season, normalize_school_name
from registration.services import add_problem, free_teams, held_schools

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
    summary: NotRequired[str]


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


IMPORT_MODES = {"replace": "Replace the school list", "add": "Add schools"}


class ImportEventForm(forms.Form):
    """The event a school list is for: an unfinalized event of the active season, or a new one; and whether the
    list replaces the school list or adds schools to the open event's."""

    event = forms.CharField(required=False)
    mode = forms.CharField(required=False)
    new_name = forms.CharField(max_length=255, required=False)
    new_date = forms.DateField(required=False)
    new_type = forms.CharField(required=False)

    # After validation: the chosen event, or an unsaved new one the import saves; and "replace" or "add"
    event_to_import: Event
    import_mode: str

    def __init__(self, *args: object, season: Season, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.season = season

    def clean(self) -> dict[str, object]:
        data = super().clean() or {}
        choice = str(data.get("event") or "")
        if choice == "new":
            if not data.get("new_name") or not data.get("new_date"):
                raise ValidationError("Give the new event a name and a date.")
            if data.get("new_type") not in dict(Event.EVENT_TYPE_CHOICES):
                raise ValidationError("Pick the new event's type.")
            # Schools come from the list, so the public registration form doesn't offer it
            self.event_to_import = Event(
                season=self.season,
                name=data["new_name"],
                date=data["new_date"],
                event_type=data["new_type"],
                registration_open=False,
            )
        elif not choice.isdecimal():
            raise ValidationError("Pick the event this school list is for.")
        else:
            chosen = self.season.events.filter(pk=int(choice), is_finalized=False).first()
            if chosen is None:
                raise ValidationError("That event was finalized or removed.")
            self.event_to_import = chosen
        self.import_mode = str(data.get("mode") or "replace")
        if self.import_mode not in IMPORT_MODES:
            raise ValidationError("Pick Replace or Add.")
        if self.import_mode == "add" and (problem := add_problem(self.event_to_import)):
            raise ValidationError(problem)
        return data


def parse_csv_file(csv_file: UploadedFile[bytes]) -> CSVParseResult:
    """Parse CSV file and validate contents."""
    rows: list[CSVRowData] = []
    errors: list[str] = []
    warnings: list[str] = []

    try:
        content = csv_file.read().decode("utf-8")
        csv_file.seek(0)

        reader = csv.DictReader(io.StringIO(content))

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
                "Columns auto-detected: " + ", ".join(f"{raw} \u2192 {canon}" for raw, canon in header_mapping.items())
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

            row_errors: list[str] = []
            row_data: CSVRowData = {}

            school_name = row.get("school_name", "").strip()
            if not school_name:
                row_errors.append(f"Row {row_num}: school_name is required")
            else:
                row_data["school_name"] = school_name

            contact_email = row.get("contact_email", "").strip()
            if not contact_email:
                row_errors.append(f"Row {row_num}: contact_email is required")
            else:
                try:
                    validate_email(contact_email)
                    row_data["contact_email"] = contact_email
                except ValidationError:
                    row_errors.append(f"Row {row_num}: contact_email is not a valid email address")

            secondary_email = row.get("secondary_email", "").strip()
            if secondary_email:
                try:
                    validate_email(secondary_email)
                    row_data["secondary_email"] = secondary_email
                except ValidationError:
                    row_errors.append(f"Row {row_num}: secondary_email is not a valid email address")
            else:
                row_data["secondary_email"] = ""

            notes = row.get("notes", "").strip()
            row_data["notes"] = notes

            if row_errors:
                errors.extend(row_errors)
            else:
                rows.append(row_data)

        if not rows and not errors:
            errors.append("CSV file contains no data rows")

    except UnicodeDecodeError:
        errors.append("File encoding error. Please ensure the file is UTF-8 encoded.")
    except Exception as e:
        errors.append(f"Error parsing CSV file: {str(e)}")

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


def validate_csv_data(rows: list[CSVRowData], event: Event | None = None) -> CSVValidationResult:
    """Assign the rows to teams 1..N in random order; the import replaces all existing school info.

    The event is the one imported into; None, or an unsaved event, is a new one.
    """
    event_id = event.pk if event is not None else None
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

    if event is not None and event_id is not None:
        replaced = event.team_assignments.count()
        if replaced:
            warnings.append(f"Replaces the {replaced} team assignment(s) for {event}.")

    return {"teams_to_create": _assign(rows, teams, event_id), "errors": errors, "warnings": warnings}


def validate_csv_additions(rows: list[CSVRowData], event: Event) -> CSVValidationResult:
    """Give the rows' schools free teams, the lowest numbers in random order, at the open event.

    Schools already on the list (or at the event) keep their teams and are skipped, so nobody moves."""
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
    return {
        "teams_to_create": _assign(new_rows, teams, event.pk),
        "errors": errors,
        "warnings": warnings,
        "summary": (
            f"Adds {len(new_rows)} school(s) to {event}; the {SchoolInfo.objects.count()} school(s) already on the "
            "list keep their team numbers."
        ),
    }
