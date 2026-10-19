from collections.abc import Iterable, Mapping

from django import forms

from core.proxmox import is_valid_snapshot_name
from core.tickets_config import TicketCategoryConfig, get_all_categories
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


HOSTNAME_MAX: int = Ticket._meta.get_field("hostname").max_length  # type: ignore[assignment]
IP_MAX: int = Ticket._meta.get_field("ip_address").max_length  # type: ignore[assignment]


class CreateTicketForm(forms.Form):
    """A new ticket, from the web form or Discord's commands and modals. Validates the category against the ones the
    caller may use, fills in hostname and IP from Quotient's service and box maps when known, and requires the
    category's required fields."""

    title = forms.CharField(max_length=255)
    description = forms.CharField(required=False)
    hostname = forms.CharField(max_length=HOSTNAME_MAX, required=False)
    ip_address = forms.CharField(max_length=IP_MAX, required=False)
    service_name = forms.CharField(max_length=100, required=False)
    category = forms.IntegerField()
    team_id = forms.IntegerField(required=False)

    category_config: TicketCategoryConfig | None = None
    category_obj: TicketCategory | None = None

    def __init__(
        self,
        *args: object,
        staff: bool = False,
        categories: Mapping[int, TicketCategoryConfig] | None = None,
        box_ip_map: Mapping[str, str] | None = None,
        service_choices: Iterable[Mapping[str, str]] = (),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.staff = staff
        self._categories = categories
        self.box_ip_map = box_ip_map or {}
        self.service_choices = service_choices

    def clean(self) -> dict[str, object]:
        cleaned = super().clean() or {}
        category_id = cleaned.get("category")
        if category_id is None:
            return cleaned
        categories = (
            self._categories if self._categories is not None else get_all_categories(user_creatable_only=not self.staff)
        )
        if category_id not in categories:
            self.add_error("category", "Invalid ticket category selected.")
            return cleaned

        cat_info = categories[category_id]
        self.category_config = cat_info
        self.category_obj = TicketCategory.objects.get(pk=category_id)

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

        for field in cat_info.get("required_fields", []):
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
