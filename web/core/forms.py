import re

from django import forms
from registration.forms import NewEventFields
from registration.models import SCHOOL_NAME_MAX_LENGTH, Event
from registration.services import NewEvent

from core.authentik_utils import parse_team_range


class SchoolInfoEditForm(forms.Form):
    school_name = forms.CharField(max_length=SCHOOL_NAME_MAX_LENGTH)
    # 254: SchoolInfo's email columns (a form EmailField alone accepts 320 characters)
    contact_email = forms.EmailField(max_length=254)
    secondary_email = forms.EmailField(max_length=254, required=False)
    notes = forms.CharField(required=False)


class BroadcastForm(forms.Form):
    target = forms.CharField()
    message = forms.CharField()


class SetMaxMembersForm(forms.Form):
    max_members = forms.IntegerField(min_value=1, max_value=20)


class TypedConfirmForm(forms.Form):
    """The word typed into a destructive action's prompt; the server checks it too."""

    confirm = forms.CharField(required=False)

    def clean_confirm(self) -> str:
        typed: str = self.cleaned_data["confirm"]
        if typed.strip().lower() != "yolo":
            raise forms.ValidationError('Type "yolo" to confirm.')
        return typed


class CancelEventForm(TypedConfirmForm):
    """The open event the page showed, and the typed confirmation."""

    open_event = forms.ModelChoiceField(
        queryset=Event.objects.all(),
        error_messages={
            "required": "No event was shown. Reload the page.",
            "invalid_choice": "That event no longer exists. Reload the page.",
        },
    )

    def first_error(self) -> str:
        return str(next(iter(self.errors.values()))[0])


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

    def first_error(self) -> str:
        return str(next(iter(self.errors.values()))[0])


class StartMessageForm(forms.Form):
    # Discord caps a message at 2000 characters; the broadcast adds an "Announcement from" header.
    MAX_LENGTH = 1900

    start_message = forms.CharField(required=False, strip=False)

    def clean_start_message(self) -> str:
        # Browsers submit textarea newlines as CRLF; any CR (with an LF or alone) is one newline, as the textarea
        # counts it, and the length is checked that way.
        message: str = re.sub(r"\r\n?", "\n", self.cleaned_data["start_message"])
        if len(message) > self.MAX_LENGTH:
            raise forms.ValidationError(f"The start message must be at most {self.MAX_LENGTH} characters")
        return message


class AppSlugForm(forms.Form):
    # An Authentik application slug: ASCII letters, digits, - and _ (so lowercasing keeps its length)
    app_slug = forms.SlugField(max_length=100)

    def clean_app_slug(self) -> str:
        return str(self.cleaned_data["app_slug"]).lower()


class SetTimeForm(forms.Form):
    datetime = forms.CharField()
    timezone = forms.CharField(initial="America/Los_Angeles")


class SetScheduleForm(forms.Form):
    start_datetime = forms.CharField(required=False)
    start_timezone = forms.CharField(required=False, initial="America/Los_Angeles")
    end_datetime = forms.CharField(required=False)
    end_timezone = forms.CharField(required=False, initial="America/Los_Angeles")

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


class ReadinessFixForm(forms.Form):
    fix = forms.CharField()


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
