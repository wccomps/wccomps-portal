import csv
import io
import re

from django import forms

from core.form_renderer import ask_for_the_file_again
from team.models import SchoolInfo

TEAM_EXTRAS_HELP = "Give a 'team' column and a row per team, with its number (like Team01 or 3)."


def parse_team_extras(csv_text: str) -> dict[str, dict[str, str]]:
    """Per-team CSV data, keyed by team number: {"1": {"api_key": "sk-...", ...}, ...}.

    The team column is the first whose header has 'team' in it; its cells give the number ("Team01" -> 1,
    "Team 3" -> 3, "5" -> 5), and a row without a number of 1 to 9 digits is skipped. Raises ValidationError
    saying what to fix when the text has no team column, no team rows, or isn't CSV.
    """
    # newline="": the csv module reads line ends itself, so CR alone, LF and CRLF all end a row
    reader = csv.DictReader(io.StringIO(csv_text.strip(), newline=""))
    result: dict[str, dict[str, str]] = {}
    try:
        team_col = next((col for col in reader.fieldnames or [] if "team" in col.lower()), None)
        if team_col is None:
            raise forms.ValidationError(f"The per-team data has no 'team' column. {TEAM_EXTRAS_HELP}")
        other_cols = [c for c in reader.fieldnames or [] if c != team_col]
        for row in reader:
            digits = re.sub(r"[^0-9]", "", row.get(team_col) or "")
            if digits and len(digits) <= 9:
                result[str(int(digits))] = {col: (row.get(col) or "").strip() for col in other_cols}
    except csv.Error as e:
        raise forms.ValidationError(
            f"The per-team data isn't CSV the upload can read (line {reader.line_num}). {TEAM_EXTRAS_HELP}"
        ) from e
    if not result:
        raise forms.ValidationError(f"No row of the per-team data has a team number. {TEAM_EXTRAS_HELP}")
    return result


class PacketUploadForm(forms.Form):
    title = forms.CharField(max_length=255, help_text="Brief description of the packet (e.g., '2026 Qualifier Packet')")
    packet_file = forms.FileField(label="File", help_text="Maximum file size: 25 MB. All file types supported.")
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Shown to teams on their packet page and in the packet email",
    )
    team_extras = forms.CharField(
        required=False,
        label="Team data CSV",
        widget=forms.Textarea(
            attrs={"rows": 6, "placeholder": "team,api_key,max_spend_usd\nTeam01,sk-abc123,100\nTeam02,sk-def456,100"}
        ),
        help_text="Paste CSV with a 'team' column header (e.g. Team01). Extra columns are included in each team's "
        "notification email. Teams without a row receive no extra data.",
    )
    send_via_email = forms.BooleanField(
        required=False,
        initial=True,
        help_text="Emails each school on the list the packet and its team's username and password (set the first "
        "time the team is sent a packet)",
    )
    web_access_enabled = forms.BooleanField(
        required=False,
        initial=True,
        label="Enable web access",
        help_text="Allow teams to download from web interface",
    )

    def clean_team_extras(self) -> dict[str, dict[str, str]]:
        text: str = self.cleaned_data["team_extras"]
        return parse_team_extras(text) if text else {}

    def clean_packet_file(self) -> object:
        f = self.cleaned_data["packet_file"]
        max_size = 25 * 1024 * 1024
        if f.size and f.size > max_size:
            raise forms.ValidationError("File size must not exceed 25 MB.")
        return f

    def clean(self) -> dict[str, object] | None:
        cleaned = super().clean()
        if self.errors:
            ask_for_the_file_again(self, "packet_file")
        return cleaned


class PacketActionForm(forms.Form):
    action = forms.CharField()
    email = forms.EmailField(required=False)
    team_id = forms.IntegerField(required=False)

    def clean(self) -> dict[str, object]:
        cleaned = super().clean() or {}
        if cleaned.get("action") == "test_email":
            if not cleaned.get("email"):
                self.add_error("email", "Email is required for test_email action.")
            if not cleaned.get("team_id"):
                self.add_error("team_id", "Team is required for test_email action.")
        return cleaned


class PacketResendForm(forms.Form):
    # A resend saves them as the team's contacts, so they stop at those columns' lengths
    primary_email = forms.EmailField(max_length=SchoolInfo._meta.get_field("contact_email").max_length)
    secondary_email = forms.EmailField(
        max_length=SchoolInfo._meta.get_field("secondary_email").max_length, required=False
    )
