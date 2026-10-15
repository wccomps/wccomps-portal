from django import forms

from core.models import INTEGER_COLUMN_MAX, INTEGER_COLUMN_MIN


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
    points_override = forms.IntegerField(required=False, min_value=INTEGER_COLUMN_MIN, max_value=INTEGER_COLUMN_MAX)


class TicketReopenForm(forms.Form):
    reopen_reason = forms.CharField(required=False)


class TicketChangeCategoryForm(forms.Form):
    new_category = forms.IntegerField()


class TicketBulkActionForm(forms.Form):
    ticket_numbers = forms.CharField()

    def clean_ticket_numbers(self) -> list[str]:
        raw = self.cleaned_data["ticket_numbers"]
        return [tn.strip() for tn in raw.split(",") if tn.strip()]
