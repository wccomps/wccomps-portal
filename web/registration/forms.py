from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Event, RegistrationContact, Season, TeamRegistration
from .services import NewEvent, NewSeason, new_event_problem

CONTACT_EMAIL_MAX_LENGTH = RegistrationContact._meta.get_field("email").max_length


class RegistrationForm(forms.ModelForm[TeamRegistration]):
    """Combined form for registration with captain, coach, events, and rules."""

    captain_name = forms.CharField(max_length=255, label="Full Name")
    # The column's length: a form EmailField alone accepts 320 characters
    captain_email = forms.EmailField(max_length=CONTACT_EMAIL_MAX_LENGTH, label="Email Address")
    captain_phone = forms.CharField(max_length=50, label="Phone Number")

    coach_name = forms.CharField(max_length=255, label="Full Name")
    coach_email = forms.EmailField(max_length=CONTACT_EMAIL_MAX_LENGTH, label="Email Address")
    coach_phone = forms.CharField(max_length=50, label="Phone Number", required=False)

    events = forms.ModelMultipleChoiceField(
        queryset=Event.objects.none(),
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

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        events_field = self.fields["events"]
        if isinstance(events_field, forms.ModelMultipleChoiceField):
            events_field.queryset = Event.objects.filter(
                season__is_active=True,
                registration_open=True,
            ).order_by("date")

    def clean(self) -> dict[str, object]:
        """Validate that non-WRCCDC teams only register for invitationals."""
        cleaned_data = super().clean() or {}
        region = cleaned_data.get("region")
        events = cleaned_data.get("events")

        if region and region != "wrccdc" and events:
            restricted_events = [e for e in events if e.event_type in ("qualifier", "regional")]
            if restricted_events:
                event_names = ", ".join(e.name for e in restricted_events)
                raise forms.ValidationError(
                    f"Only Western Regional (WRCCDC) teams can register for Qualifiers and Regionals. "
                    f"Please remove: {event_names}"
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
            RegistrationEventEnrollment.objects.filter(registration=registration).exclude(event__in=selected).delete()
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
