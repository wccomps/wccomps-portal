from collections.abc import Iterable
from typing import cast

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.utils.html import format_html

from .models import Event, RegistrationContact, RegistrationEventEnrollment, Season, TeamRegistration
from .services import NewEvent, NewSeason, new_event_problem

CONTACT_EMAIL_MAX_LENGTH = RegistrationContact._meta.get_field("email").max_length
RESTRICTED_EVENT_TYPES = ("qualifier", "regional")
RULES_URL = "https://wrccdc.org/about/rules/"
PHONE = "(555) 123-4567"
EMAIL = "name@school.edu"


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
    captain_email = forms.EmailField(
        max_length=CONTACT_EMAIL_MAX_LENGTH,
        label="Email Address",
        widget=forms.EmailInput(attrs={"placeholder": EMAIL}),
    )
    captain_phone = forms.CharField(
        max_length=50, label="Phone Number", widget=forms.TextInput(attrs={"type": "tel", "placeholder": PHONE})
    )

    coach_name = forms.CharField(max_length=255, label="Full Name")
    coach_email = forms.EmailField(
        max_length=CONTACT_EMAIL_MAX_LENGTH,
        label="Email Address",
        widget=forms.EmailInput(attrs={"placeholder": EMAIL}),
    )
    coach_phone = forms.CharField(
        max_length=50,
        label="Phone Number",
        required=False,
        widget=forms.TextInput(attrs={"type": "tel", "placeholder": PHONE}),
    )

    # Built once, at import; each form runs its own copy, and the deadline is checked when that runs
    events = OpenEventsField(
        queryset=Event.objects.open_for_registration().order_by("date"),
        widget=forms.CheckboxSelectMultiple,
        required=True,
        label="Select all events your team plans to participate in",
        error_messages={"required": "Select at least one event."},
    )

    agree_to_rules = forms.BooleanField(
        required=True,
        label=format_html(
            'I have read and agree to the <a href="{}" target="_blank">competition rules and guidelines</a>',
            RULES_URL,
        ),
        error_messages={"required": "Agree to the competition rules to register."},
    )

    class Meta:
        model = TeamRegistration
        fields = ["school_name", "region"]
        labels = {
            "school_name": "School / University Name",
            "region": "CCDC Region",
        }
        help_texts = {
            "school_name": "Enter the full official name of your institution.",
            "region": "Select your school's CCDC region. Only WRCCDC teams can register for Qualifiers and Regionals.",
        }
        widgets = {"school_name": forms.TextInput(attrs={"placeholder": "e.g. Riverdale State University"})}

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        events_field = self.fields["events"]
        self.events_open = cast(QuerySet[Event], cast(OpenEventsField, events_field).queryset).exists()
        if not self.events_open:
            events_field.help_text = "No events are open for registration right now. Check back later."

    def clean(self) -> dict[str, object]:
        """No Qualifier or Regional for a non-WRCCDC team."""
        cleaned_data = super().clean() or {}
        region = cleaned_data.get("region")
        events = cast(Iterable[Event], cleaned_data.get("events") or [])

        if region and region != "wrccdc":
            restricted = [e.name for e in events if e.event_type in RESTRICTED_EVENT_TYPES]
            if restricted:
                self.add_error(
                    "events",
                    "Only Western Regional (WRCCDC) teams can register for Qualifiers and Regionals. "
                    f"Please remove: {', '.join(restricted)}",
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
    """An event to create and the season the page showed as active; new_event() raises the problems no single field
    holds."""

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
    current_season = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def new_season(self) -> NewSeason | None:
        """The new season for the event; only the import starts one."""
        return None

    def new_event(self) -> NewEvent:
        """The event to create; call from clean() once the fields it reads have no errors of their own."""
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
