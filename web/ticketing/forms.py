from django import forms

from ticketing.models import Ticket, TicketCategory

TICKET_FIELD_CHOICES = [
    ("hostname", "Hostname"),
    ("ip_address", "IP Address"),
    ("service_name", "Service Name"),
    ("description", "Description"),
]


class TicketCategoryForm(forms.ModelForm[TicketCategory]):
    """A ticket category, on the ops category page and in the admin. Its limits are the model's; the minimum,
    maximum and sort order may be left blank, for 0."""

    required_fields = forms.MultipleChoiceField(
        choices=TICKET_FIELD_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="Fields the user must fill in when creating a ticket.",
    )
    optional_fields = forms.MultipleChoiceField(
        choices=TICKET_FIELD_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="Fields shown but not required.",
    )

    class Meta:
        model = TicketCategory
        fields = "__all__"
        widgets = {
            "variable_cost_note": forms.Textarea(
                attrs={
                    "placeholder": 'e.g. "If consultation exceeded 45 minutes, '
                    'ticket lead will manually adjust to 300 points"',
                }
            ),
            "playbook": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": "1. Verify service status\n2. Check credentials\n3. Restart daemon",
                }
            ),
        }

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        for name in ("min_points", "max_points", "sort_order"):
            self.fields[name].required = False
        if self.instance.pk:
            self.initial["required_fields"] = self.instance.required_fields
            self.initial["optional_fields"] = self.instance.optional_fields

    def clean_required_fields(self) -> list[str]:
        return list(self.cleaned_data.get("required_fields", []))

    def clean_optional_fields(self) -> list[str]:
        return list(self.cleaned_data.get("optional_fields", []))

    def clean_min_points(self) -> int:
        return self.cleaned_data.get("min_points") or 0

    def clean_max_points(self) -> int:
        return self.cleaned_data.get("max_points") or 0

    def clean_sort_order(self) -> int:
        return self.cleaned_data.get("sort_order") or 0

    def clean_playbook(self) -> str:
        return (self.cleaned_data.get("playbook") or "").strip()


class CreateTicketForm(forms.Form):
    title = forms.CharField(max_length=255)
    description = forms.CharField(required=False)
    hostname = forms.CharField(max_length=255, required=False)
    ip_address = forms.CharField(max_length=45, required=False)
    service_name = forms.CharField(max_length=100, required=False)
    category = forms.IntegerField()
    team_id = forms.IntegerField(required=False)


class TicketCommentForm(forms.Form):
    comment = forms.CharField()


class TicketReassignForm(forms.Form):
    new_assignee_username = forms.CharField(max_length=150)


class TicketResolveForm(forms.Form):
    resolution_notes = forms.CharField(required=False)
    # The bounds of the column the points are charged to
    points_override = forms.IntegerField(required=False, validators=Ticket._meta.get_field("points_charged").validators)


class TicketReopenForm(forms.Form):
    reopen_reason = forms.CharField(required=False)


class TicketChangeCategoryForm(forms.Form):
    new_category = forms.IntegerField()


class TicketBulkActionForm(forms.Form):
    ticket_numbers = forms.CharField()

    def clean_ticket_numbers(self) -> list[str]:
        raw = self.cleaned_data["ticket_numbers"]
        numbers = [tn.strip() for tn in raw.split(",") if tn.strip()]
        if not numbers:
            raise forms.ValidationError("No tickets selected")
        return numbers
