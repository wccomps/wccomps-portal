from django import forms
from registration.forms import NewEventFields
from registration.models import Event
from registration.services import NewEvent

from core.authentik_utils import parse_team_range


class SchoolInfoEditForm(forms.Form):
    school_name = forms.CharField(max_length=255)
    contact_email = forms.EmailField()
    secondary_email = forms.EmailField(required=False)
    notes = forms.CharField(required=False)


class BroadcastForm(forms.Form):
    target = forms.CharField()
    message = forms.CharField()


class CategoryForm(forms.Form):
    display_name = forms.CharField(max_length=100)
    points = forms.IntegerField(initial=0)
    required_fields = forms.TypedMultipleChoiceField(
        coerce=str,
        required=False,
        choices=[
            ("hostname", "Hostname"),
            ("ip_address", "IP Address"),
            ("service_name", "Service Name"),
            ("description", "Description"),
        ],
    )
    optional_fields = forms.TypedMultipleChoiceField(
        coerce=str,
        required=False,
        choices=[
            ("hostname", "Hostname"),
            ("ip_address", "IP Address"),
            ("service_name", "Service Name"),
            ("description", "Description"),
        ],
    )
    variable_points = forms.BooleanField(required=False)
    variable_cost_note = forms.CharField(required=False)
    min_points = forms.IntegerField(initial=0, required=False)
    max_points = forms.IntegerField(initial=0, required=False)
    user_creatable = forms.BooleanField(required=False)
    sort_order = forms.IntegerField(initial=0, required=False)
    playbook = forms.CharField(required=False, widget=forms.Textarea)

    def clean_min_points(self) -> int:
        return self.cleaned_data.get("min_points") or 0

    def clean_max_points(self) -> int:
        return self.cleaned_data.get("max_points") or 0

    def clean_sort_order(self) -> int:
        return self.cleaned_data.get("sort_order") or 0

    def clean_playbook(self) -> str:
        return (self.cleaned_data.get("playbook") or "").strip()


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
        # Browsers submit textarea newlines as CRLF; the length is checked as the textarea counts it.
        message: str = self.cleaned_data["start_message"].replace("\r\n", "\n")
        if len(message) > self.MAX_LENGTH:
            raise forms.ValidationError(f"The start message must be at most {self.MAX_LENGTH} characters")
        return message


class AppSlugForm(forms.Form):
    app_slug = forms.CharField(max_length=100)

    def clean_app_slug(self) -> str:
        slug: str = self.cleaned_data["app_slug"].strip().lower()
        if not slug:
            raise forms.ValidationError("App slug is required")
        return slug


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
