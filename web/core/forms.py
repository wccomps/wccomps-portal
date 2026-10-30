import re

from django import forms
from registration.forms import NewEventFields
from registration.models import SCHOOL_NAME_MAX_LENGTH, Event
from registration.services import NewEvent

from core.authentik_utils import parse_team_range
from core.models import MAX_TEAM_MEMBERS_LIMIT
from core.utils import DEFAULT_TIMEZONE
from team.models import SchoolInfo


class SchoolInfoEditForm(forms.Form):
    RENAME = "rename"
    NEW_SCHOOL = "new_school"

    # The school the page showed, so an edit made from a stale page is refused
    shown_school = forms.IntegerField(required=False, widget=forms.HiddenInput)
    shown_key = forms.CharField(required=False, widget=forms.HiddenInput)
    school_name = forms.CharField(
        label="School Name",
        max_length=SCHOOL_NAME_MAX_LENGTH,
        widget=forms.TextInput(attrs={"class": "vTextField", "placeholder": "e.g., University of Example"}),
    )
    change = forms.ChoiceField(
        label="If the name changed",
        choices=[(RENAME, "A rename of the same school"), (NEW_SCHOOL, "A different school")],
        required=False,
        widget=forms.RadioSelect,
        help_text="A rename keeps the team's members, Discord channels and password. A different school "
        "unlinks the team's Discord members, removes its Discord role and channels, and resets its Authentik password "
        "and sessions, so it starts clean.",
    )
    # The columns' lengths: a form EmailField alone accepts 320 characters
    contact_email = forms.EmailField(
        label="Contact Email",
        max_length=SchoolInfo._meta.get_field("contact_email").max_length,
        help_text="Primary contact for this team",
        widget=forms.EmailInput(attrs={"class": "vTextField", "placeholder": "e.g., coach@example.edu"}),
    )
    secondary_email = forms.EmailField(
        label="Secondary Email",
        max_length=SchoolInfo._meta.get_field("secondary_email").max_length,
        required=False,
        help_text="Optional secondary contact",
        widget=forms.EmailInput(attrs={"class": "vTextField", "placeholder": "e.g., alternate@example.edu"}),
    )
    notes = forms.CharField(
        required=False,
        help_text="Optional internal notes",
        widget=forms.Textarea(
            attrs={"class": "vLargeTextField", "rows": 4, "placeholder": "Additional notes about this team..."}
        ),
    )


class BroadcastForm(forms.Form):
    target = forms.CharField()
    message = forms.CharField()


class SetMaxMembersForm(forms.Form):
    max_members = forms.IntegerField(min_value=1, max_value=MAX_TEAM_MEMBERS_LIMIT)


class TypedConfirmForm(forms.Form):
    """The word typed into a destructive action's prompt; the server checks it too."""

    confirm = forms.CharField(required=False)

    def clean_confirm(self) -> str:
        typed: str = self.cleaned_data["confirm"]
        if typed.strip().lower() != "yolo":
            raise forms.ValidationError('Type "yolo" to confirm.')
        return typed

    def first_error(self) -> str:
        return str(next(iter(self.errors.values()))[0])


class CancelEventForm(TypedConfirmForm):
    """The open event the page showed, and the typed confirmation."""

    open_event = forms.ModelChoiceField(
        queryset=Event.objects.all(),
        error_messages={
            "required": "No event was shown. Reload the page.",
            "invalid_choice": "That event no longer exists. Reload the page.",
        },
    )


class CloseOutForm(TypedConfirmForm, NewEventFields):
    """What the page showed: the open event (its id), or with none open the event Close Out records the scores at
    in the active season the page showed; and the typed confirmation."""

    open_event = forms.ModelChoiceField(
        queryset=Event.objects.all(),
        required=False,
        error_messages={"invalid_choice": "That event no longer exists. Reload the page."},
    )

    def clean(self) -> dict[str, object]:
        data = super().clean() or {}
        if not self.errors and not data.get("open_event"):
            self.new_event_to_create = self.new_event()
        return data

    def target(self) -> Event | NewEvent:
        """The page's open event, or the new event to create. Call once is_valid() is True."""
        event: Event | None = self.cleaned_data.get("open_event")
        return event if event is not None else self.new_event_to_create


class StartMessageForm(forms.Form):
    # Discord caps a message at 2000 characters; the broadcast adds an "Announcement from" header.
    MAX_LENGTH = 1900

    start_message = forms.CharField(required=False, strip=False)

    def clean_start_message(self) -> str:
        # Count newlines as the textarea does: CRLF or a lone CR is one LF
        message: str = re.sub(r"\r\n?", "\n", self.cleaned_data["start_message"])
        if len(message) > self.MAX_LENGTH:
            raise forms.ValidationError(f"The start message must be at most {self.MAX_LENGTH} characters")
        return message


class AppSlugForm(forms.Form):
    # An Authentik application slug: ASCII letters, digits, - and _ (so lowercasing keeps its length)
    app_slug = forms.SlugField(max_length=100)

    def clean_app_slug(self) -> str:
        return str(self.cleaned_data["app_slug"]).lower()


class SetScheduleForm(forms.Form):
    start_datetime = forms.CharField(required=False)
    start_timezone = forms.CharField(required=False, initial=DEFAULT_TIMEZONE)
    end_datetime = forms.CharField(required=False)
    end_timezone = forms.CharField(required=False, initial=DEFAULT_TIMEZONE)

    def clean(self) -> dict[str, str]:
        cleaned = super().clean()
        if cleaned is None:
            return {}
        start = (cleaned.get("start_datetime") or "").strip()
        end = (cleaned.get("end_datetime") or "").strip()
        cleaned["start_datetime"] = start
        cleaned["end_datetime"] = end
        if not start and not end:
            raise forms.ValidationError("Please set at least one time")
        return cleaned


class ResetPasswordsForm(forms.Form):
    team_numbers = forms.CharField(required=False)

    def clean_team_numbers(self) -> list[int] | None:
        raw = self.cleaned_data.get("team_numbers", "").strip()
        if not raw:
            return None
        try:
            return parse_team_range(raw)
        except ValueError as e:
            raise forms.ValidationError(str(e)) from e


class ActionForm(forms.Form):
    action = forms.CharField()
    readiness_fix = forms.BooleanField(required=False)


class TeamActionForm(forms.Form):
    action = forms.CharField()
    discord_id = forms.IntegerField(required=False)


class TeamsBulkActionForm(forms.Form):
    action = forms.CharField()
    team_numbers = forms.CharField()

    def clean_team_numbers(self) -> list[int]:
        raw = self.cleaned_data["team_numbers"]
        try:
            return parse_team_range(raw)
        except ValueError as e:
            raise forms.ValidationError(str(e)) from e


class LinkConfirmForm(forms.Form):
    token = forms.CharField(max_length=64)


class SyncRolesForm(forms.Form):
    dry_run = forms.CharField(required=False)

    def clean_dry_run(self) -> bool:
        # Anything but an explicit "false" is a preview
        value: str = self.cleaned_data.get("dry_run") or ""
        return value.strip().lower() != "false"
