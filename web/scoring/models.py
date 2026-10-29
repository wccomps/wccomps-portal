from decimal import Decimal
from typing import NamedTuple

from django.contrib.auth.models import User
from django.core.files.uploadedfile import UploadedFile
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from core.models import Approvable
from core.utils import shorten


def format_boxes_display(boxes: list[str] | str | None) -> str:
    """Format an affected_boxes field value for display."""
    if not boxes:
        return ""
    if isinstance(boxes, str):
        return boxes
    return ", ".join(boxes)


def validate_file_size(file: UploadedFile[bytes]) -> UploadedFile[bytes]:
    max_size_mb = 50
    if file.size and file.size > max_size_mb * 1024 * 1024:
        from django.core.exceptions import ValidationError

        raise ValidationError(f"File size cannot exceed {max_size_mb}MB")
    return file


class ScoringTemplate(models.Model):
    """Scoring configuration using category weights and raw maximums (scaled via calculator._get_modifiers)."""

    service_weight = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("40"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text="Percentage weight for service uptime",
    )
    inject_weight = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("40"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text="Percentage weight for inject responses",
    )
    orange_weight = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("20"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text="Percentage weight for orange team checks",
    )

    # Maximum possible raw points in each category
    service_max = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("11454"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Maximum possible raw service points",
    )
    inject_max = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("3060"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Maximum possible raw inject points",
    )
    orange_max = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("160"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Maximum possible raw orange team check points",
    )

    def clean(self) -> None:
        """Validate that the category weights sum to 100."""
        from django.core.exceptions import ValidationError

        total = (self.service_weight or 0) + (self.inject_weight or 0) + (self.orange_weight or 0)
        if total != Decimal("100"):
            raise ValidationError(f"Weights must sum to 100 (currently {total})")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scoring_template_updates",
    )

    class Meta:
        db_table = "scoring_template"
        verbose_name = "Scoring Template"
        verbose_name_plural = "Scoring Templates"

    def __str__(self) -> str:
        return "Scoring Template"


class QuotientMetadataCache(models.Model):
    """Cached metadata from Quotient for populating dropdowns."""

    boxes = models.JSONField(
        default=list,
        help_text="List of boxes from Quotient metadata",
    )
    services = models.JSONField(
        default=list,
        help_text="List of services from Quotient metadata",
    )

    last_synced = models.DateTimeField(auto_now=True)
    synced_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "quotient_metadata_cache"
        verbose_name = "Quotient Metadata Cache"
        verbose_name_plural = "Quotient Metadata Caches"

    def __str__(self) -> str:
        return "Quotient Metadata Cache"


class RedTeamIPPool(models.Model):
    """Reusable pool of source IP addresses for red team members."""

    name = models.CharField(max_length=100)
    ip_addresses = models.TextField(help_text="Newline or comma-separated IP addresses used for rotating attacks")
    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="ip_pools",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "red_team_ip_pool"
        verbose_name = "Red Team IP Pool"
        verbose_name_plural = "Red Team IP Pools"
        unique_together = [["name", "created_by"]]
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.ip_count} IPs)"

    @property
    def ip_count(self) -> int:
        return len(self.get_ip_list())

    def get_ip_list(self) -> list[str]:
        if not self.ip_addresses:
            return []
        ips = []
        for line in self.ip_addresses.replace(",", "\n").split("\n"):
            ip = line.strip()
            if ip:
                ips.append(ip)
        return ips

    def contains_ip(self, ip: str) -> bool:
        return ip.strip() in self.get_ip_list()


class AttackType(models.Model):
    """Predefined attack types for red team findings."""

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, help_text="Help text for red teamers")
    is_active = models.BooleanField(default=True, help_text="Can be selected for new findings")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "attack_type"
        verbose_name = "Attack Type"
        verbose_name_plural = "Attack Types"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Outcome(NamedTuple):
    """One attack outcome a red team finding can claim: its model field and the points each affected team loses."""

    field: str
    points: int
    label: str
    description: str
    # An access outcome's rank, 0 the highest: a finding scores only the highest one it reached
    access_tier: int | None = None

    @property
    def display(self) -> str:
        return f"{self.label} (-{self.points})"


class RedTeamScore(Approvable):
    """Red team vulnerability score affecting one or more teams."""

    OUTCOMES: tuple[Outcome, ...] = (
        Outcome("root_access", 100, "Root Access", "Root/Administrator level access obtained", access_tier=0),
        Outcome(
            "user_access", 25, "User Access", "User level access obtained (not scored with root access)", access_tier=1
        ),
        Outcome(
            "privilege_escalation", 100, "Privilege Escalation", "User escalated to Root/Admin (additional penalty)"
        ),
        Outcome("credentials_recovered", 50, "Credentials", "User IDs and passwords recovered"),
        Outcome("sensitive_files_recovered", 25, "Sensitive Files", "Config files, corporate data recovered"),
        Outcome("credit_cards_recovered", 50, "Credit Cards", "Customer credit card numbers recovered"),
        Outcome("pii_recovered", 200, "PII", "PII recovered: name, address, credit card number"),
        Outcome("encrypted_db_recovered", 25, "Encrypted DB", "Encrypted customer data/database recovered"),
        Outcome("db_decrypted", 25, "DB Decrypted", "Database was decrypted (additional penalty)"),
    )
    OUTCOME_HELP = {outcome.field: outcome.description for outcome in OUTCOMES}

    submitted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="red_scores_submitted",
    )

    attack_type = models.ForeignKey(
        AttackType,
        on_delete=models.PROTECT,
        related_name="findings",
        null=True,
        blank=True,
    )
    source_ip = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text="Red team source IP (use this OR source_ip_pool)",
    )
    source_ip_pool = models.ForeignKey(
        RedTeamIPPool,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="findings",
        help_text="Pool of rotating source IPs (use this OR source_ip)",
    )
    destination_ip_template = models.TextField(
        blank=True,
        help_text="IP template (e.g., 10.100.1X.22 where X is team number)",
    )

    # Affected infrastructure (from Quotient metadata)
    affected_boxes = models.JSONField(
        default=list,
        blank=True,
        help_text="List of affected box names",
    )
    affected_service = models.CharField(max_length=100, blank=True)

    universally_attempted = models.BooleanField(
        default=False,
        help_text="Attack was attempted against all teams",
    )
    persistence_established = models.BooleanField(
        default=False,
        help_text="Persistence was established on target",
    )

    # Outcome checkboxes - cumulative deductions per CCDC scoring guidelines
    # Access levels (only highest scored per attack)
    root_access = models.BooleanField(
        default=False,
        help_text=OUTCOME_HELP["root_access"],
    )
    user_access = models.BooleanField(
        default=False,
        help_text=OUTCOME_HELP["user_access"],
    )
    privilege_escalation = models.BooleanField(
        default=False,
        help_text=OUTCOME_HELP["privilege_escalation"],
    )

    # Data recovery outcomes (cumulative)
    credentials_recovered = models.BooleanField(
        default=False,
        help_text=OUTCOME_HELP["credentials_recovered"],
    )
    sensitive_files_recovered = models.BooleanField(
        default=False,
        help_text=OUTCOME_HELP["sensitive_files_recovered"],
    )
    credit_cards_recovered = models.BooleanField(
        default=False,
        help_text=OUTCOME_HELP["credit_cards_recovered"],
    )
    pii_recovered = models.BooleanField(
        default=False,
        help_text=OUTCOME_HELP["pii_recovered"],
    )
    encrypted_db_recovered = models.BooleanField(
        default=False,
        help_text=OUTCOME_HELP["encrypted_db_recovered"],
    )
    db_decrypted = models.BooleanField(
        default=False,
        help_text=OUTCOME_HELP["db_decrypted"],
    )

    affected_teams = models.ManyToManyField(
        "team.Team",
        related_name="red_team_scores",
        help_text="Teams affected by this finding",
    )
    points_per_team = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Points deducted per affected team (auto-calculated from outcomes)",
    )

    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "red_team_finding"
        verbose_name = "Red Team Score"
        verbose_name_plural = "Red Team Scores"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"RF-{self.pk}: {self.attack_type.name if self.attack_type else 'Unknown'}"

    @property
    def source_ip_display(self) -> str:
        """Return display string for source IP (single IP or pool name)."""
        if self.source_ip:
            return str(self.source_ip)
        if self.source_ip_pool:
            return f"Pool: {self.source_ip_pool.name} ({self.source_ip_pool.ip_count} IPs)"
        return "No source IP"

    @property
    def affected_boxes_display(self) -> str:
        return format_boxes_display(self.affected_boxes)

    def scored_outcomes(self) -> list[Outcome]:
        """The outcomes this finding scores, in table order."""
        reached = [outcome for outcome in self.OUTCOMES if getattr(self, outcome.field)]
        top = min((o.access_tier for o in reached if o.access_tier is not None), default=None)
        return [outcome for outcome in reached if outcome.access_tier in (None, top)]

    def calculate_points(self) -> Decimal:
        """Points each affected team loses from the outcome checkboxes (stored positive; calculator subtracts it)."""
        return sum((Decimal(outcome.points) for outcome in self.scored_outcomes()), Decimal("0"))

    @property
    def outcomes_display(self) -> list[str]:
        return [outcome.display for outcome in self.scored_outcomes()]


class RedTeamScreenshot(models.Model):
    """Screenshots for red team findings (stored in database)."""

    finding = models.ForeignKey(
        RedTeamScore,
        on_delete=models.CASCADE,
        related_name="screenshots",
    )
    file_data = models.BinaryField(null=True, blank=True)
    filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100, default="image/png")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "red_team_screenshot"
        ordering = ["uploaded_at"]

    def __str__(self) -> str:
        return f"{self.filename} ({self.finding})"


class IncidentReport(Approvable):
    """Blue team incident report submission."""

    team = models.ForeignKey(
        "team.Team",
        on_delete=models.CASCADE,
        related_name="incident_reports",
        help_text="Team submitting the incident report",
    )
    submitted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="incidents_submitted",
    )

    attack_description = models.TextField()
    source_ip = models.GenericIPAddressField()
    destination_ip = models.GenericIPAddressField(null=True, blank=True)

    affected_boxes = models.JSONField(
        default=list,
        blank=True,
        help_text="List of affected box names",
    )
    affected_service = models.CharField(max_length=100, blank=True)

    attack_detected_at = models.DateTimeField(help_text="When the attack was detected")
    attack_mitigated = models.BooleanField(
        default=False,
        help_text="Attack was successfully mitigated",
    )

    matched_to_red_score = models.ForeignKey(
        RedTeamScore,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="matched_incident_reports",
        help_text="Red team score this incident matched to",
    )
    points_returned = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0"),
        help_text="Points awarded for detecting/reporting this incident",
    )
    approval_notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "incident_report"
        verbose_name = "Incident Report"
        verbose_name_plural = "Incident Reports"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team"]),
            models.Index(fields=["is_approved"]),
        ]

    def __str__(self) -> str:
        return f"IR-{self.pk} ({self.team.team_name}): {shorten(self.attack_description, 50)}"

    @property
    def affected_boxes_display(self) -> str:
        return format_boxes_display(self.affected_boxes)


class IncidentScreenshot(models.Model):
    """Screenshots for incident reports (stored in database)."""

    incident = models.ForeignKey(
        IncidentReport,
        on_delete=models.CASCADE,
        related_name="screenshots",
    )
    file_data = models.BinaryField(null=True, blank=True)
    filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100, default="image/png")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "incident_screenshot"
        ordering = ["uploaded_at"]

    def __str__(self) -> str:
        return f"{self.filename} ({self.incident})"


class InjectScore(Approvable):
    """White/Gold team scoring of inject submissions."""

    team = models.ForeignKey(
        "team.Team",
        on_delete=models.CASCADE,
        related_name="inject_grades",
    )

    # Inject info (inject definitions come from Quotient)
    inject_id = models.CharField(max_length=100, help_text="Quotient inject ID")
    inject_name = models.CharField(max_length=200)
    max_points = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    points_awarded = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )

    feedback = models.TextField(
        blank=True,
        help_text="Professional feedback shown to teams on scorecard",
    )
    feedback_approved = models.BooleanField(
        default=False,
        help_text="Whether feedback has been approved for team viewing",
    )
    feedback_approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inject_feedback_approved",
        help_text="User who approved this feedback",
    )

    graded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="injects_graded",
    )
    graded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # View-layer annotations (not DB fields) — set by outlier calculation in views
    is_outlier: bool = False
    std_devs_from_mean: float = 0.0

    class Meta:
        db_table = "inject_grade"
        verbose_name = "Inject Score"
        verbose_name_plural = "Inject Scores"
        unique_together = [["team", "inject_id"]]
        ordering = ["inject_name", "team__team_number"]

    def __str__(self) -> str:
        if self.max_points:
            return f"{self.team.team_name}, {self.inject_name}: {self.points_awarded}/{self.max_points}"
        return f"{self.team.team_name}, {self.inject_name}: {self.points_awarded}"


class OrangeTeamScore(Approvable):
    """Orange team check scores from customer service evaluations."""

    team = models.ForeignKey(
        "team.Team",
        on_delete=models.CASCADE,
        related_name="orange_team_scores",
    )
    submitted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="orange_scores_submitted",
    )

    description = models.TextField(help_text="Description of the orange team check")
    points_awarded = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Points awarded for the check",
    )

    orange_check = models.ForeignKey(
        "orange_team.OrangeCheck",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="orange_scores",
        help_text="Orange check this score was created from",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "orange_team_bonus"
        verbose_name = "Orange Team Check"
        verbose_name_plural = "Orange Team Checks"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        sign = "+" if self.points_awarded >= 0 else ""
        return f"{self.team.team_name}, {shorten(self.description, 50)}: {sign}{self.points_awarded}"


class ServiceScore(models.Model):
    """Service uptime scores from Quotient."""

    team = models.ForeignKey(
        "team.Team",
        on_delete=models.CASCADE,
        related_name="service_scores",
    )

    service_points = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0"),
    )
    sla_violations = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0"),
        help_text="SLA penalty points (stored as negative)",
    )

    synced_at = models.DateTimeField(auto_now=True)
    synced_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "service_score"
        verbose_name = "Service Score"
        verbose_name_plural = "Service Scores"
        unique_together = [["team"]]
        ordering = ["team__team_number"]

    def __str__(self) -> str:
        return f"{self.team.team_name}: {self.service_points} (SLA: {self.sla_violations})"


class ServiceDetail(models.Model):
    """Per-service uptime scores (e.g., balrog-ntp, brassknuckles-imap)."""

    team = models.ForeignKey(
        "team.Team",
        on_delete=models.CASCADE,
        related_name="service_details",
    )
    service_name = models.CharField(max_length=100, help_text="Service identifier (e.g., balrog-ntp)")
    points = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    uptime = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        default=Decimal("0"),
        help_text="Uptime percentage (0.0000 to 1.0000)",
    )
    sla_violations = models.IntegerField(
        default=0,
        help_text="Number of SLA violations",
    )

    class Meta:
        db_table = "service_detail"
        verbose_name = "Service Detail"
        verbose_name_plural = "Service Details"
        unique_together = [["team", "service_name"]]
        ordering = ["service_name", "team__team_number"]

    def __str__(self) -> str:
        return f"{self.team.team_name}, {self.service_name}: {self.points}"


class ScoringExclusion(models.Model):
    """A team left off the leaderboard and out of comparative stats; its own score still computes."""

    team = models.OneToOneField("team.Team", on_delete=models.CASCADE, related_name="scoring_exclusion")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "scoring_exclusion"

    def __str__(self) -> str:
        return f"{self.team.team_name} excluded"
