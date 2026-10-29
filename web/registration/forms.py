from collections.abc import Iterable
from typing import cast

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet

from .models import Event, RegistrationContact, RegistrationEventEnrollment, Season, TeamRegistration
from .services import NewEvent, NewSeason, new_event_problem

CONTACT_EMAIL_MAX_LENGTH = RegistrationContact._meta.get_field("email").max_length
RESTRICTED_EVENT_TYPES = ("qualifier", "regional")


class OpenEventsField(forms.ModelMultipleChoiceField[Event]):
    """Names a ticked event that isn't open, where Django's invalid choice error gives only the posted id."""

    def clean(self, value: object) -> QuerySet[Event]:
        try:
            return super().clean(value)
        except ValidationError as error:
            pk = (error.params or {}).get("value") if error.code == "invalid_choice" else None
            offered = cast(QuerySet[Event], self.queryset).values_list("pk", flat=True)
            closed = Event.objects.filter(pk=pk).exclude(pk__in=offered).first() if pk else None
            if closed is None:
                raise
            message = f"{closed.name} is no longer open for registration."
            raise ValidationError(message, code="invalid_choice") from error


class RegistrationForm(forms.ModelForm[TeamRegistration]):
    """Combined form for registration with captain, coach, events, and rules."""

    captain_name = forms.CharField(max_length=255, label="Full Name")
    # The column's length: a form EmailField alone accepts 320 characters
    captain_email = forms.EmailField(max_length=CONTACT_EMAIL_MAX_LENGTH, label="Email Address")
    captain_phone = forms.CharField(max_length=50, label="Phone Number")

    coach_name = forms.CharField(max_length=255, label="Full Name")
    coach_email = forms.EmailField(max_length=CONTACT_EMAIL_MAX_LENGTH, label="Email Address")
    coach_phone = forms.CharField(max_length=50, label="Phone Number", required=False)

    # Built once, at import; each form runs its own copy, and the deadline is checked when that runs
    events = OpenEventsField(
        queryset=Event.objects.open_for_registration().order_by("date"),
        widget=forms.CheckboxSelectMultiple,
        required=True,
        label="Select Events",
    )

    agree_to_rules = forms.BooleanField(
        required=True,
        label="I agree to the competition rules",
    )

    class Meta:
        model = TeamRegistration
        fields = ["school_name", "region"]
        labels = {
            "school_name": "School Name",
            "region": "CCDC Region",
        }
        help_texts = {
            "region": "Select your school's CCDC region. Only WRCCDC teams can register for Qualifiers and Regionals.",
        }

    def clean(self) -> dict[str, object]:
        """No Qualifier or Regional for a non-WRCCDC team."""
        cleaned_data = super().clean() or {}
        region = cleaned_data.get("region")
        events = cast(Iterable[Event], cleaned_data.get("events") or [])

        if region and region != "wrccdc":
            restricted = [e.name for e in events if e.event_type in RESTRICTED_EVENT_TYPES]
            if restricted:
                raise forms.ValidationError(
                    "Only Western Regional (WRCCDC) teams can register for Qualifiers and Regionals. "
                    f"Please remove: {', '.join(restricted)}"
                )

        return cleaned_data

    @transaction.atomic
    def save(self, commit: bool = True) -> TeamRegistration:
        if not commit:
            raise ValueError("RegistrationForm can't save without commit: its contacts and enrollments need the row")
        registration = super().save()
        RegistrationContact.objects.create(
            registration=registration,
            role="captain",
            name=self.cleaned_data["captain_name"],
            email=self.cleaned_data["captain_email"],
            phone=self.cleaned_data["captain_phone"],
        )
        RegistrationContact.objects.create(
            registration=registration,
            role="coach",
            name=self.cleaned_data["coach_name"],
            email=self.cleaned_data["coach_email"],
            phone=self.cleaned_data.get("coach_phone", ""),
        )
        RegistrationEventEnrollment.objects.bulk_create(
            RegistrationEventEnrollment(registration=registration, event=event) for event in self.cleaned_data["events"]
        )
        return registration


class SeasonForm(forms.ModelForm[Season]):
    """A season's name and year; checking Active Season activates it (Season.activate), which deactivates the
    others. An active season stays active until another is activated."""

    is_active = forms.BooleanField(
        required=False,
        label="Active Season",
        help_text="Makes this the active season; the season active now stops being active.",
    )

    class Meta:
        model = Season
        fields = ["name", "year"]
        labels = {
            "name": "Season Name",
            "year": "Year",
        }

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        if self.instance.is_active:
            self.fields["is_active"].initial = True
            self.fields["is_active"].disabled = True

    @transaction.atomic
    def save(self, commit: bool = True) -> Season:
        """Save the season and, when checked, activate it, together."""
        season = super().save(commit=commit)
        if commit and self.cleaned_data["is_active"] and not season.is_active:
            season.activate()
        return season


class EventForm(forms.ModelForm[Event]):
    class Meta:
        model = Event
        fields = [
            "name",
            "event_type",
            "event_number",
            "date",
            "start_time",
            "end_time",
            "registration_open",
            "registration_deadline",
            "max_teams",
        ]
        labels = {
            "name": "Event Name",
            "event_type": "Event Type",
            "event_number": "Event Number",
            "date": "Event Date",
            "start_time": "Start Time",
            "end_time": "End Time",
            "registration_open": "Accept registrations",
            "registration_deadline": "Registration Deadline",
            "max_teams": "Maximum Teams",
        }
        help_texts = {
            "registration_open": "Registration also closes when the deadline passes, when the event is closed out, "
            "or while its season isn't the active one.",
        }
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
            "registration_deadline": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        }

    def validate_constraints(self) -> None:
        """Check the model's constraints with the instance's season: it isn't a field (the view sets it), so
        ModelForm's own check would leave out every constraint on it. A clash shows on the event number."""
        if {"event_type", "event_number"} & set(self.errors):
            return
        try:
            self.instance.validate_constraints()
        except ValidationError as clash:
            self.add_error("event_number", clash.messages)


class RejectRegistrationForm(forms.Form):
    reason = forms.CharField()


class ShownSchoolForm(forms.Form):
    """The school a Remove or Unassign page showed, posted back so the removal can check it is still the team's."""

    school = forms.IntegerField(min_value=1)

    def expected_school_id(self) -> int:
        """The posted school, or 0 (no school) when missing, so the removal is refused as a change."""
        return self.cleaned_data["school"] if self.is_valid() else 0


class NewEventFields(forms.Form):
    """The fields of an event to create (the import's new event, Close Out's event for scores that ran without an
    import) and the season the page showed as active. new_event() checks them together, as non-field errors."""

    NEW_FIELDS = ("new_name", "new_date", "new_type", "current_season")
    MISSING = "Give the new event a name, a date and a type."

    new_name = forms.CharField(
        max_length=255, required=False, error_messages={"max_length": "The event name must be at most 255 characters."}
    )
    new_date = forms.DateField(required=False, error_messages={"invalid": "Give the new event a valid date."})
    new_type = forms.ChoiceField(
        choices=Event.EVENT_TYPE_CHOICES,
        required=False,
        error_messages={"invalid_choice": "Choose a valid event type."},
    )
    # The id of the season the page showed as active, empty when none was
    current_season = forms.IntegerField(required=False)

    def new_season(self) -> NewSeason | None:
        """The new season for the event; only the import starts one."""
        return None

    def new_event(self) -> NewEvent:
        """The event to create, from the cleaned fields. Call from clean(); ValidationError says what is wrong."""
        for name in self.NEW_FIELDS:
            if name in self.errors:
                raise forms.ValidationError(self.errors[name][0])
        data = self.cleaned_data
        if not (data.get("new_name") and data.get("new_date") and data.get("new_type")):
            raise forms.ValidationError(self.MISSING)
        new_event = NewEvent(
            name=data["new_name"],
            date=data["new_date"],
            event_type=data["new_type"],
            seen_season=data.get("current_season"),
            season=self.new_season(),
        )
        if problem := new_event_problem(new_event):
            raise forms.ValidationError(problem)
        return new_event
