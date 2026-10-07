import ipaddress
from typing import cast

from django import forms
from django.contrib.auth.models import User
from django.db.models import QuerySet

from team.models import Team

from .models import (
    AttackType,
    IncidentReport,
    RedTeamIPPool,
    RedTeamScore,
    ScoringTemplate,
)
from .quotient_sync import get_box_choices, get_service_choices


class TeamNumberChoiceField(forms.ModelMultipleChoiceField["Team"]):
    """Show 'Team N' instead of team names to maintain impartiality."""

    def label_from_instance(self, obj: Team) -> str:
        return f"Team {obj.team_number}"


class RedTeamIPPoolForm(forms.ModelForm[RedTeamIPPool]):
    class Meta:
        model = RedTeamIPPool
        fields = ["name", "ip_addresses"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "placeholder": "e.g., Rotation Pool A",
                    "class": "form-control",
                }
            ),
            "ip_addresses": forms.Textarea(
                attrs={
                    "rows": 10,
                    "placeholder": "Enter IP addresses, one per line or comma-separated:\n10.0.0.1\n10.0.0.2\n10.0.0.3",
                    "class": "form-control",
                }
            ),
        }
        labels = {
            "name": "Pool Name",
            "ip_addresses": "IP Addresses",
        }
        help_texts = {
            "name": "A memorable name for this pool of IPs",
            "ip_addresses": "Enter one IP per line or comma-separated. Invalid IPs will be rejected.",
        }

    def __init__(self, *args: object, user: User | None = None, **kwargs: object) -> None:
        self.user = user
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def clean_name(self) -> str:
        """Validate pool name is unique for this user."""
        name: str = self.cleaned_data["name"]
        if self.user:
            existing = RedTeamIPPool.objects.filter(name__iexact=name, created_by=self.user)
            if self.instance and self.instance.pk:
                existing = existing.exclude(pk=self.instance.pk)
            if existing.exists():
                raise forms.ValidationError("You already have a pool with this name")
        return name

    def clean_ip_addresses(self) -> str:
        """Validate and normalize IP addresses."""
        raw = self.cleaned_data["ip_addresses"]
        if not raw:
            raise forms.ValidationError("At least one IP address is required")

        valid_ips = []
        invalid_ips = []
        for line in raw.replace(",", "\n").split("\n"):
            ip = line.strip()
            if not ip:
                continue
            try:
                ipaddress.ip_address(ip)
                valid_ips.append(ip)
            except ValueError:
                invalid_ips.append(ip)

        if invalid_ips:
            raise forms.ValidationError(f"Invalid IP addresses: {', '.join(invalid_ips[:5])}")

        if not valid_ips:
            raise forms.ValidationError("At least one valid IP address is required")

        return "\n".join(valid_ips)


class RedTeamScoreForm(forms.ModelForm[RedTeamScore]):
    """Form for red team to submit vulnerability findings."""

    source_ip_type = forms.ChoiceField(
        choices=[
            ("single", "Single IP"),
            ("pool", "IP Pool"),
        ],
        initial="single",
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
        label="Source IP Type",
    )

    # Multi-select for affected boxes (stored as JSON list, hidden — toggle buttons in template)
    affected_boxes = forms.MultipleChoiceField(
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "d-none", "id": "id_affected_boxes"}),
        label="Affected Boxes",
    )

    class Meta:
        model = RedTeamScore
        fields = [
            "attack_type",
            "source_ip",
            "source_ip_pool",
            "affected_service",
            "destination_ip_template",
            "universally_attempted",
            "persistence_established",
            "root_access",
            "user_access",
            "privilege_escalation",
            "credentials_recovered",
            "sensitive_files_recovered",
            "credit_cards_recovered",
            "pii_recovered",
            "encrypted_db_recovered",
            "db_decrypted",
            "affected_teams",
            "notes",
        ]
        widgets = {
            "attack_type": forms.Select(attrs={"class": "form-select"}),
            "affected_teams": forms.CheckboxSelectMultiple(),
            "notes": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": "Full description of the attack and steps taken...",
                    "class": "form-control",
                }
            ),
            "destination_ip_template": forms.TextInput(attrs={"readonly": "readonly", "class": "form-control"}),
            "source_ip": forms.TextInput(attrs={"placeholder": "e.g., 10.0.0.5", "class": "form-control"}),
            "source_ip_pool": forms.Select(attrs={"class": "form-select"}),
        }
        labels = {
            "attack_type": "Attack Type",
            "source_ip": "Red Team Source IP",
            "source_ip_pool": "IP Pool",
            "affected_service": "Affected Service",
            "destination_ip_template": "Target IP Template (auto-populated)",
            "universally_attempted": "Attempted against all teams",
            "persistence_established": "Persistence established",
            "affected_teams": "Affected Teams (select all that apply)",
            "notes": "Description",
        }
        help_texts = {
            "attack_type": "Select the type of attack performed.",
            "source_ip": "The IP address you attacked from.",
            "source_ip_pool": "Select a pool of rotating IPs.",
            "universally_attempted": "Check if this attack was attempted against all teams.",
            "persistence_established": "Check if you established persistent access.",
            "affected_teams": "Select all teams that were successfully compromised.",
            "notes": "Full description of the attack and steps taken.",
        }

    def __init__(
        self, *args: object, team_count: int | None = None, user: User | None = None, **kwargs: object
    ) -> None:
        self.user = user
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

        attack_type_field = cast("forms.ModelChoiceField[AttackType]", self.fields["attack_type"])
        attack_type_field.queryset = AttackType.objects.filter(is_active=True)
        attack_type_field.empty_label = "Select attack type..."
        attack_type_field.required = True

        # Make source_ip and source_ip_pool not required at form level (validated in clean)
        self.fields["source_ip"].required = False
        self.fields["source_ip_pool"].required = False

        pool_field = cast("forms.ModelChoiceField[RedTeamIPPool]", self.fields["source_ip_pool"])
        if user:
            pool_field.queryset = RedTeamIPPool.objects.filter(created_by=user)
        else:
            pool_field.queryset = RedTeamIPPool.objects.none()
        pool_field.empty_label = "Select a pool..."

        self.box_choices = get_box_choices()
        cast(forms.MultipleChoiceField, self.fields["affected_boxes"]).choices = self.box_choices

        if self.instance and self.instance.pk and self.instance.affected_boxes:
            boxes = self.instance.affected_boxes
            self.initial["affected_boxes"] = boxes if isinstance(boxes, list) else [boxes]

        # Service dropdown - all services initially, JS filters by selected boxes
        service_choices = [("", "Select a service...")] + get_service_choices()
        self.fields["affected_service"] = forms.ChoiceField(
            required=False,
            choices=service_choices,
            widget=forms.Select(attrs={"class": "form-select"}),
            label="Affected Service",
        )

        # Replace default field with TeamNumberChoiceField to hide team names
        queryset = Team.objects.filter(is_active=True).order_by("team_number")
        if team_count is not None:
            queryset = queryset.filter(team_number__lte=team_count)
        self.fields["affected_teams"] = TeamNumberChoiceField(
            queryset=queryset,
            widget=forms.CheckboxSelectMultiple(),
            label="Affected Teams (select all that apply)",
            help_text="Select all teams that were successfully compromised.",
        )

    def clean(self) -> dict[str, object]:
        """Validate that either source_ip or source_ip_pool is provided."""
        cleaned_data = super().clean() or {}
        source_ip_type = cleaned_data.get("source_ip_type")
        source_ip = cleaned_data.get("source_ip")
        source_ip_pool = cleaned_data.get("source_ip_pool")

        if source_ip_type == "single":
            if not source_ip:
                self.add_error("source_ip", "Source IP is required when using single IP mode")
            cleaned_data["source_ip_pool"] = None
        elif source_ip_type == "pool":
            if not source_ip_pool:
                self.add_error("source_ip_pool", "Please select an IP pool")
            cleaned_data["source_ip"] = None

        return cleaned_data

    def save(self, commit: bool = True) -> RedTeamScore:
        instance = super().save(commit=False)
        # SelectMultiple already returns a list
        instance.affected_boxes = self.cleaned_data.get("affected_boxes", [])
        instance.points_per_team = instance.calculate_points()
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class IncidentReportForm(forms.ModelForm[IncidentReport]):
    """Form for blue teams to submit incident reports."""

    team = forms.ModelChoiceField(
        queryset=Team.objects.none(),
        required=False,
        label="Team",
    )

    # Multi-select for affected boxes (stored as JSON list, hidden — toggle buttons in template)
    affected_boxes = forms.MultipleChoiceField(
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "d-none", "id": "id_affected_boxes"}),
        label="Affected Boxes",
    )

    class Meta:
        model = IncidentReport
        fields = [
            "destination_ip",
            "affected_service",
            "source_ip",
            "attack_detected_at",
            "attack_description",
            "attack_mitigated",
        ]
        labels = {
            "destination_ip": "Affected IP",
            "affected_service": "Affected Service (optional)",
            "source_ip": "Attacker IP",
            "attack_detected_at": "When Detected",
            "attack_description": "What Happened",
            "attack_mitigated": "Attack Mitigated",
        }
        help_texts = {
            "destination_ip": "",
            "affected_service": "",
            "source_ip": "",
            "attack_detected_at": "",
            "attack_description": "",
            "attack_mitigated": "Check if you've stopped the attack",
        }
        widgets = {
            "attack_description": forms.Textarea(attrs={"rows": 3}),
            "attack_detected_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "destination_ip": forms.TextInput(attrs={"readonly": "readonly"}),
        }

    def __init__(self, team: Team | None = None, is_admin: bool = False, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

        if is_admin:
            team_field = cast("forms.ModelChoiceField[Team]", self.fields["team"])
            team_field.queryset = Team.objects.filter(is_active=True).order_by("team_number")
            team_field.required = True
            team_field.widget.attrs["id"] = "id_team"
            if team:
                team_field.initial = team
            self.order_fields(
                [
                    "team",
                    "affected_boxes",
                    "destination_ip",
                    "affected_service",
                    "source_ip",
                    "attack_detected_at",
                    "attack_description",
                    "attack_mitigated",
                ]
            )
        else:
            del self.fields["team"]

        self.box_choices = get_box_choices()
        cast(forms.MultipleChoiceField, self.fields["affected_boxes"]).choices = self.box_choices

        # Service dropdown - all services initially, JS filters by selected boxes
        service_choices = [("", "Select a service...")] + get_service_choices()
        self.fields["affected_service"] = forms.ChoiceField(
            required=False,
            choices=service_choices,
            widget=forms.Select(attrs={"class": "form-select"}),
            label="Affected Service",
        )

        if self.instance and self.instance.pk and self.instance.affected_boxes:
            boxes = self.instance.affected_boxes
            self.initial["affected_boxes"] = boxes if isinstance(boxes, list) else [boxes]

    def save(self, commit: bool = True) -> IncidentReport:
        instance = super().save(commit=False)
        # SelectMultiple already returns a list
        instance.affected_boxes = self.cleaned_data.get("affected_boxes", [])
        if commit:
            instance.save()
        return instance


class IncidentMatchForm(forms.ModelForm[IncidentReport]):
    """Form for gold team to match incident reports to red team findings."""

    class Meta:
        model = IncidentReport
        fields = [
            "matched_to_red_score",
            "points_returned",
            "approval_notes",
        ]
        widgets = {
            "approval_notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(
        self, suggested_findings: QuerySet[RedTeamScore] | None = None, *args: object, **kwargs: object
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

        if suggested_findings:
            matched_field = cast("forms.ModelChoiceField[RedTeamScore]", self.fields["matched_to_red_score"])
            # Validation filters the queryset, which a sliced one does not allow.
            matched_field.queryset = RedTeamScore.objects.filter(pk__in=[f.pk for f in suggested_findings])
            matched_field.empty_label = "No match / Manual points"


class ScoringTemplateForm(forms.ModelForm[ScoringTemplate]):
    """Form for configuring scoring weights. The raw maxima are derived from competition
    data (service sync, inject grade/metadata sync, orange checks), so they are not edited
    here; the config page shows them read-only."""

    class Meta:
        model = ScoringTemplate
        fields = [
            "service_weight",
            "inject_weight",
            "orange_weight",
        ]
        weight_attrs = {"min": "0", "max": "100", "step": "1", "class": "form-control"}
        widgets = {
            "service_weight": forms.NumberInput(attrs=weight_attrs),
            "inject_weight": forms.NumberInput(attrs=weight_attrs),
            "orange_weight": forms.NumberInput(attrs=weight_attrs),
        }


class InjectGradingForm(forms.Form):
    inject_id = forms.CharField(max_length=100)

    def __init__(self, *args: object, team_numbers: list[int] | None = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        if team_numbers:
            for num in team_numbers:
                # The column's bounds: points_awarded is DecimalField(max_digits=10, decimal_places=2) >= 0
                self.fields[f"points_team_{num}"] = forms.DecimalField(
                    required=False, min_value=0, max_digits=10, decimal_places=2
                )
                # What the page showed when it loaded: a field the grader didn't change is left alone
                self.fields[f"loaded_team_{num}"] = forms.DecimalField(required=False, max_digits=10, decimal_places=2)
                self.fields[f"feedback_team_{num}"] = forms.CharField(required=False)


class SaveInjectFeedbackForm(forms.Form):
    score_id = forms.IntegerField()
    feedback = forms.CharField(required=False)


class ApproveInjectFeedbackForm(forms.Form):
    score_id = forms.IntegerField()


class ScorecardEmailForm(forms.Form):
    custom_message = forms.CharField(required=False, widget=forms.Textarea)

    def get_custom_message(self) -> str:
        """Return stripped custom message if form is valid, else empty string."""
        if self.is_valid():
            return str(self.cleaned_data.get("custom_message", "")).strip()
        return ""
