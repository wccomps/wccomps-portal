from collections.abc import Iterable
from typing import cast

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.http import QueryDict
from django.utils.html import format_html

from .models import Event, RegistrationContact, Season, TeamRegistration
from .services import NewEvent, NewSeason, new_event_problem

CONTACT_EMAIL_MAX_LENGTH = RegistrationContact._meta.get_field("email").max_length
RESTRICTED_EVENT_TYPES = ("qualifier", "regional")
RULES_URL = "https://wrccdc.org/about/rules/"
PHONE = "(555) 123-4567"
EMAIL = "name@school.edu"
# The registration edit page always posts this beside the open events it offered (shown_events)
LISTED = "events_listed"


def open_events() -> QuerySet[Event]:
    return Event.objects.filter(season__is_active=True, registration_open=True).order_by("date")


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

    events = OpenEventsField(
        queryset=Event.objects.none(),
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
        events_field = cast(OpenEventsField, self.fields["events"])
        offered = open_events()
        events_field.queryset = offered
        offered_pks = set(offered.values_list("pk", flat=True))
        self.events_open = bool(offered_pks)
        if not offered_pks:
            events_field.help_text = "No events are open for registration right now. Check back later."
        self.unoffered_events: list[Event] = []
        self.untickable: set[int] = set()
        if self.instance.pk:
            enrolled = set(self.instance.event_enrollments.values_list("event_id", flat=True))
            self.untickable = offered_pks
            if self.is_bound and LISTED in self.data:
                # The edit page posts the open events it offered: only those can be unticked
                listed = {int(v) for v in self._posted("shown_events") if v.isascii() and v.isdigit()}
                self.untickable = offered_pks & listed
                if unlisted := (enrolled & offered_pks) - listed:
                    # Opened since the page loaded: a re-render shows them ticked, as the team has them
                    self._post("events", [*self._posted("events"), *map(str, unlisted)])
            self.unoffered_events = [
                enrollment.event
                for enrollment in self.instance.event_enrollments.select_related("event")
                .exclude(event_id__in=offered_pks)
                .order_by("event__date")
            ]
            # An enrollment a save can't untick already registers the team for something
            events_field.required = bool(offered_pks) and not (enrolled - self.untickable)

    def _posted(self, name: str) -> list[str]:
        if isinstance(self.data, QueryDict):
            return self.data.getlist(name)
        values = self.data.get(name, [])
        return [str(v) for v in (values if isinstance(values, list | tuple) else [values])]

    def _post(self, name: str, values: list[str]) -> None:
        if isinstance(self.data, QueryDict):
            data = self.data.copy()
            data.setlist(name, values)
            self.data = data
        else:
            self.data = {**self.data, name: values}

    def clean(self) -> dict[str, object]:
        """No Qualifier or Regional for a non-WRCCDC team, counting the enrollments an edit keeps."""
        cleaned_data = super().clean() or {}
        region = cleaned_data.get("region")
        events = list(cast(Iterable[Event], cleaned_data.get("events") or []))
        # An open event the team holds is among the ticked ones (__init__ adds any the page didn't list)
        kept = self.unoffered_events

        if region and region != "wrccdc":
            restricted = [e.name for e in events if e.event_type in RESTRICTED_EVENT_TYPES]
            closed = [e.name for e in kept if e.event_type in RESTRICTED_EVENT_TYPES]
            if restricted:
                self.add_error(
                    "events",
                    "Only Western Regional (WRCCDC) teams can register for Qualifiers and Regionals. "
                    f"Please remove: {', '.join(restricted)}",
                )
            if closed:
                # Unticking can't fix it, so it belongs to no field
                self.add_error(
                    None,
                    f"This team is registered for {', '.join(closed)}, which only WRCCDC teams can enter and which "
                    "can't be changed here: contact the organizers to change the region.",
                )

        return cleaned_data

    def save(self, commit: bool = True) -> TeamRegistration:
        """Save registration, contacts, and event enrollments."""
        from .models import RegistrationEventEnrollment

        editing = self.instance.pk is not None
        registration = super().save(commit=commit and not editing)
        if commit:
            if editing:
                if registration.status == "rejected":
                    registration.status = "pending"
                    registration.rejection_reason = ""
                    registration.save(update_fields=["school_name", "region", "status", "rejection_reason"])
                else:
                    registration.save(update_fields=["school_name", "region"])
            RegistrationContact.objects.update_or_create(
                registration=registration,
                role="captain",
                defaults={
                    "name": self.cleaned_data["captain_name"],
                    "email": self.cleaned_data["captain_email"],
                    "phone": self.cleaned_data["captain_phone"],
                },
            )
            RegistrationContact.objects.update_or_create(
                registration=registration,
                role="coach",
                defaults={
                    "name": self.cleaned_data["coach_name"],
                    "email": self.cleaned_data["coach_email"],
                    "phone": self.cleaned_data.get("coach_phone", ""),
                },
            )
            selected = list(self.cleaned_data["events"])
            RegistrationEventEnrollment.objects.filter(registration=registration, event_id__in=self.untickable).exclude(
                event__in=selected
            ).delete()
            for event in selected:
                RegistrationEventEnrollment.objects.get_or_create(
                    registration=registration,
                    event=event,
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
            "registration_open": "Registration Open",
            "registration_deadline": "Registration Deadline",
            "max_teams": "Maximum Teams",
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
    import) and the season the page showed as active. new_event() checks that they are all given and fit together,
    as non-field errors."""

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
