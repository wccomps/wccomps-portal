from django import forms

from .models import Event, RegistrationContact, Season, TeamRegistration


class RegistrationForm(forms.ModelForm[TeamRegistration]):
    """Combined form for registration with captain, coach, events, and rules."""

    captain_name = forms.CharField(max_length=255, label="Full Name")
    captain_email = forms.EmailField(label="Email Address")
    captain_phone = forms.CharField(max_length=50, label="Phone Number")

    coach_name = forms.CharField(max_length=255, label="Full Name")
    coach_email = forms.EmailField(label="Email Address")
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

        registration = super().save(commit=commit)
        if commit:
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
            for event in self.cleaned_data["events"]:
                RegistrationEventEnrollment.objects.get_or_create(
                    registration=registration,
                    event=event,
                )
        return registration


class SeasonForm(forms.ModelForm[Season]):
    class Meta:
        model = Season
        fields = ["name", "year", "is_active"]
        labels = {
            "name": "Season Name",
            "year": "Year",
            "is_active": "Active Season",
        }
        help_texts = {
            "is_active": "Only one season should be active at a time.",
        }


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


class RejectRegistrationForm(forms.Form):
    reason = forms.CharField()


class ShownSchoolForm(forms.Form):
    """The school a Remove or Unassign page showed, posted back so the removal can check it is still the team's."""

    school = forms.IntegerField(min_value=1)

    def expected_school_id(self) -> int:
        """The posted school, or 0 (no school) when missing, so the removal is refused as a change."""
        return self.cleaned_data["school"] if self.is_valid() else 0
