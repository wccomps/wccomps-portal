from collections.abc import Iterable, Mapping
from typing import cast

from django import forms
from django.db.models import QuerySet

from core.proxmox import is_valid_snapshot_name
from core.widgets import ToggleButtonSelect
from team.models import Team
from ticketing.models import Ticket, TicketCategory

TICKET_FIELD_CHOICES = [
    ("hostname", "Hostname"),
    ("ip_address", "IP Address"),
    ("service_name", "Service Name"),
    ("description", "Description"),
]

REQUIRED_FIELD_ERRORS = {
    "hostname": "Hostname is required for this category.",
    "ip_address": "IP Address is required for this category.",
    "service_name": "Service Name is required for this category.",
    "description": "Description is required for this category.",
}


class TicketCategoryForm(forms.ModelForm[TicketCategory]):
    """A ticket category, on the ops category page and in the admin. Its limits are the model's; the minimum,
    maximum and sort order may be left blank, for 0."""

    required_fields = forms.MultipleChoiceField(
        choices=TICKET_FIELD_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Required fields",
        help_text="Fields the user must fill in when creating a ticket.",
    )
    optional_fields = forms.MultipleChoiceField(
        choices=TICKET_FIELD_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Optional fields",
        help_text="Fields shown but not required.",
    )

    class Meta:
        model = TicketCategory
        fields = "__all__"
        labels = {
            "points": "Default points",
            "playbook": "Playbook steps",
            "variable_points": "Points vary per ticket",
            "user_creatable": "Users can create tickets with this category",
        }
        help_texts = {
            "playbook": "Optional step-by-step instructions for staff responding to tickets in this category "
            "(one step per line).",
            "variable_cost_note": "Shown to users when creating tickets for this category.",
            "min_points": "Only applies when Points vary per ticket is ticked.",
            "max_points": "Only applies when Points vary per ticket is ticked.",
        }
        error_messages = {"display_name": {"required": "Display name is required."}}
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
        optional = list(self.cleaned_data.get("optional_fields", []))
        required = self.cleaned_data.get("required_fields", [])
        both = [label for value, label in TICKET_FIELD_CHOICES if value in optional and value in required]
        if both:
            verb = "is" if len(both) == 1 else "are"
            raise forms.ValidationError(
                f"{', '.join(both)} {verb} already required; a field is either required or optional."
            )
        return optional

    def clean_min_points(self) -> int:
        return self.cleaned_data.get("min_points") or 0

    def clean_max_points(self) -> int:
        return self.cleaned_data.get("max_points") or 0

    def clean_sort_order(self) -> int:
        return self.cleaned_data.get("sort_order") or 0

    def clean_playbook(self) -> str:
        return (self.cleaned_data.get("playbook") or "").strip()


HOSTNAME_MAX: int = Ticket._meta.get_field("hostname").max_length  # type: ignore[assignment]
IP_MAX: int = Ticket._meta.get_field("ip_address").max_length  # type: ignore[assignment]


class TeamChoiceField(forms.ModelChoiceField[Team]):
    def label_from_instance(self, obj: Team) -> str:
        return f"Team {obj.team_number}"


def _category_label(category: TicketCategory) -> str:
    worth = "Variable" if category.variable_points else f"{category.points}pt" if category.points else "Free"
    return f"{category.display_name} ({worth})"


class CreateTicketForm(forms.Form):
    """A new ticket, from the web form or Discord's commands and modals. Validates the category against the ones the
    caller may use, fills in hostname and IP from Quotient's service and box maps when known, and requires the
    category's required fields.

    Widget attrs bind to create_ticket.html's Alpine state."""

    title = forms.CharField(
        max_length=255,
        help_text="Brief summary of the issue",
        widget=forms.TextInput(attrs={"placeholder": "Brief summary of the issue"}),
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={"rows": 8, ":required": "descriptionRequired", "placeholder": "Detailed description of the issue..."}
        ),
    )
    hostname = forms.CharField(
        max_length=HOSTNAME_MAX,
        required=False,
        widget=forms.TextInput(
            attrs={
                ":value": "selectedHostname",
                "x-on:input": "setField",
                "data-field": "selectedHostname",
                ":required": "hostnameRequired",
                "placeholder": "e.g. web01",
            }
        ),
    )
    ip_address = forms.CharField(
        max_length=IP_MAX,
        required=False,
        label="IP Address",
        help_text="Enter the box's IP address",
        widget=forms.TextInput(
            attrs={
                ":value": "ipAddress",
                "x-on:input": "setField",
                "data-field": "ipAddress",
                ":required": "ipRequired",
                "placeholder": "e.g. 10.0.1.5",
            }
        ),
    )
    service_name = forms.CharField(
        max_length=100,
        required=False,
        label="Service",
        widget=forms.TextInput(
            attrs={
                ":value": "selectedService",
                "x-on:input": "setField",
                "data-field": "selectedService",
                ":required": "serviceRequired",
                "placeholder": "e.g. HTTP",
            }
        ),
    )
    category = forms.IntegerField(
        widget=forms.Select(attrs={":value": "selectedCategory", "x-on:change": "onCategoryChange"})
    )
    # Staff pick the team when the caller passes teams; without them it is off and a posted team is ignored
    team_id = TeamChoiceField(
        Team.objects.none(),
        required=False,
        disabled=True,
        label="Team",
        empty_label="Select a team...",
        error_messages={"required": "Please select a team.", "invalid_choice": "Please select a team."},
    )

    category_obj: TicketCategory | None = None

    def __init__(
        self,
        *args: object,
        staff: bool = False,
        categories: Iterable[TicketCategory] | None = None,
        box_ip_map: Mapping[str, str] | None = None,
        service_choices: Iterable[Mapping[str, str]] = (),
        teams: QuerySet[Team] | None = None,
        box_names: Iterable[str] = (),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.staff = staff
        self._categories = categories
        self.box_ip_map = box_ip_map or {}
        self.service_choices = service_choices
        if self._categories is not None:
            self.fields["category"].widget.choices = [
                ("", "Select a category..."),
                *((cat.pk, _category_label(cat)) for cat in self._categories),
            ]
        if teams:
            team_id = cast(TeamChoiceField, self.fields["team_id"])
            team_id.queryset = teams
            team_id.required = True
            team_id.disabled = False
        if box_names := list(box_names):
            self.fields["hostname"].widget = ToggleButtonSelect(
                attrs={"x-on:change": "pickHostname"}, choices=[("", ""), *((box, box) for box in box_names)]
            )
            ip_address = self.fields["ip_address"]
            ip_address.help_text = "Auto-filled from hostname selection"
            ip_address.widget.attrs["placeholder"] = "Select a hostname above"
        if self.service_choices:
            self.fields["service_name"].widget = forms.Select(
                choices=[("", "Select a service...")],
                attrs={
                    "x-ref": "serviceSelect",
                    ":value": "selectedService",
                    "x-on:change": "onServiceChange",
                    ":required": "serviceRequired",
                },
            )

    def clean(self) -> dict[str, object]:
        cleaned = super().clean() or {}
        category_id = cleaned.get("category")
        if category_id is None:
            return cleaned
        allowed = (
            {cat.pk: cat for cat in self._categories}
            if self._categories is not None
            else {
                cat.pk: cat
                for cat in (
                    TicketCategory.objects.all() if self.staff else TicketCategory.objects.filter(user_creatable=True)
                )
            }
        )
        if category_id not in allowed:
            self.add_error("category", "Invalid ticket category selected.")
            return cleaned

        category = allowed[category_id]
        self.category_obj = category

        hostname = str(cleaned.get("hostname", ""))
        ip_address = str(cleaned.get("ip_address", ""))
        service_name = str(cleaned.get("service_name", ""))
        if service_name and not hostname:
            for svc in self.service_choices:
                if svc.get("value") == service_name:
                    hostname = svc.get("box_name", "")[:HOSTNAME_MAX]
                    if not ip_address:
                        ip_address = svc.get("box_ip", "")[:IP_MAX]
                    break
        if hostname and not ip_address:
            ip_address = self.box_ip_map.get(hostname, "")[:IP_MAX]
        cleaned["hostname"] = hostname
        cleaned["ip_address"] = ip_address

        for field in category.required_fields:
            if field in REQUIRED_FIELD_ERRORS and field not in self.errors and not cleaned.get(field):
                self.add_error(field, REQUIRED_FIELD_ERRORS[field])
        return cleaned


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
    # Not required: no selection gets the one error below, which the bulk views show
    ticket_numbers = forms.CharField(required=False)

    def clean_ticket_numbers(self) -> list[str]:
        raw = self.cleaned_data["ticket_numbers"]
        numbers = [tn.strip() for tn in raw.split(",") if tn.strip()]
        if not numbers:
            raise forms.ValidationError("No tickets selected")
        return numbers


class TicketBoxResetForm(forms.Form):
    snapshot = forms.CharField(max_length=128)

    def clean_snapshot(self) -> str:
        snapshot = str(self.cleaned_data["snapshot"]).strip()
        if not is_valid_snapshot_name(snapshot):
            raise forms.ValidationError("Invalid snapshot name.")
        return snapshot
