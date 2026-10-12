import random

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.auth_utils import require_permission
from core.utils import client_ip
from team.models import SchoolInfo, Team

from .forms import EventForm, RegistrationForm, RejectRegistrationForm, SeasonForm, ShownSchoolForm
from .models import (
    Event,
    EventTeamAssignment,
    RegistrationEventEnrollment,
    RegistrationRateLimit,
    Season,
    TeamRegistration,
    normalize_school_name,
)
from .services import (
    PacketsSentError,
    SchoolChangedError,
    SchoolTakenError,
    TeamTakenError,
    activate_teams,
    packets_sent,
    remove_team_school,
    set_team_school,
)


def register(request: HttpRequest) -> HttpResponse:
    """Public registration form (no authentication required)."""
    if request.method == "POST":
        # Rate limit: 10 submissions per hour per client IP
        ip = client_ip(request)
        if RegistrationRateLimit.over_limit(ip):
            messages.error(request, "Too many registration submissions. Please try again later.")
            return render(request, "registration/register.html", {"form": RegistrationForm()}, status=429)

        form = RegistrationForm(request.POST)
        if form.is_valid():
            RegistrationRateLimit.record(ip)  # Only count successful submissions
            form.save()
            messages.success(request, "Registration submitted successfully!")
            return redirect("registration_register")
    else:
        form = RegistrationForm()

    return render(request, "registration/register.html", {"form": form})


def registration_edit(request: HttpRequest, token: str) -> HttpResponse:
    """Token-based self-service editing of registration."""
    registration = get_object_or_404(TeamRegistration, edit_token=token)

    if registration.edit_token_expires and timezone.now() > registration.edit_token_expires:
        messages.error(request, "This edit link has expired.")
        return render(request, "registration/edit_locked.html", {"registration": registration})

    # Lock after approval so school name, region (event restrictions) and contacts can't be changed or hijacked.
    if registration.status not in ("pending", "rejected"):
        messages.error(request, "This registration can no longer be edited after approval.")
        return render(request, "registration/edit_locked.html", {"registration": registration})

    if request.method == "POST":
        form = RegistrationForm(request.POST, instance=registration)
        if form.is_valid():
            form.save()
            messages.success(request, "Registration updated successfully!")
            return redirect("registration_edit", token=token)
    else:
        captain = registration.contacts.filter(role="captain").first()
        coach = registration.contacts.filter(role="coach").first()
        enrolled_events = list(registration.event_enrollments.values_list("event_id", flat=True))
        initial = {"events": enrolled_events, "agree_to_rules": True}
        if captain:
            initial.update(
                {
                    "captain_name": captain.name,
                    "captain_email": captain.email,
                    "captain_phone": captain.phone,
                }
            )
        if coach:
            initial.update(
                {
                    "coach_name": coach.name,
                    "coach_email": coach.email,
                    "coach_phone": coach.phone,
                }
            )
        form = RegistrationForm(instance=registration, initial=initial)

    return render(request, "registration/edit.html", {"form": form, "registration": registration})


@require_permission("gold_team")
def review_list(request: HttpRequest) -> HttpResponse:
    status_filter = request.GET.get("status", "")

    registrations = TeamRegistration.objects.prefetch_related("contacts", "event_enrollments__event").all()
    if status_filter:
        registrations = registrations.filter(status=status_filter)

    context = {
        "registrations": registrations,
        "status_filter": status_filter,
        "status_choices": TeamRegistration.STATUS_CHOICES,
        "subnav_active": "registrations",
    }

    if request.headers.get("HX-Request"):
        return render(request, "cotton/registration_review_table.html", context)

    return render(request, "registration/review_list.html", context)


@require_permission("gold_team")
def approve_registration(request: HttpRequest, registration_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("registration_review_list")

    registration = get_object_or_404(TeamRegistration, id=registration_id)
    if request.user.is_authenticated:
        registration.approve(request.user)
    messages.success(request, f"Registration for {registration.school_name} has been approved.")

    return redirect("registration_review_list")


@require_permission("gold_team")
def reject_registration(request: HttpRequest, registration_id: int) -> HttpResponse:
    registration = get_object_or_404(TeamRegistration, id=registration_id)

    context = {"registration": registration, "subnav_active": "registrations"}

    if request.method == "POST":
        form = RejectRegistrationForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Please provide a reason for rejection.")
            return render(request, "registration/reject_confirm.html", context)
        reason = form.cleaned_data["reason"]

        registration.reject(reason)
        messages.success(request, f"Registration for {registration.school_name} has been rejected.")
        return redirect("registration_review_list")

    return render(request, "registration/reject_confirm.html", context)


@require_permission("gold_team")
def mark_paid(request: HttpRequest, registration_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("registration_review_list")

    registration = get_object_or_404(TeamRegistration, id=registration_id)
    registration.mark_as_paid()
    messages.success(request, f"Registration for {registration.school_name} marked as paid.")

    return redirect("registration_review_list")


@require_permission("gold_team")
def season_list(request: HttpRequest) -> HttpResponse:
    seasons = Season.objects.prefetch_related("events").all()
    return render(request, "registration/seasons/list.html", {"seasons": seasons, "subnav_active": "seasons"})


@require_permission("gold_team")
def season_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = SeasonForm(request.POST)
        if form.is_valid():
            season = form.save()
            messages.success(request, f"Season '{season.name}' created successfully.")
            return redirect("registration_season_list")
    else:
        form = SeasonForm()

    return render(
        request, "registration/seasons/form.html", {"form": form, "title": "Create Season", "subnav_active": "seasons"}
    )


@require_permission("gold_team")
def season_edit(request: HttpRequest, season_id: int) -> HttpResponse:
    season = get_object_or_404(Season, id=season_id)

    if request.method == "POST":
        form = SeasonForm(request.POST, instance=season)
        if form.is_valid():
            form.save()
            messages.success(request, f"Season '{season.name}' updated successfully.")
            return redirect("registration_season_list")
    else:
        form = SeasonForm(instance=season)

    return render(
        request,
        "registration/seasons/form.html",
        {"form": form, "title": "Edit Season", "season": season, "subnav_active": "seasons"},
    )


@require_permission("gold_team")
def season_delete(request: HttpRequest, season_id: int) -> HttpResponse:
    season = get_object_or_404(Season, id=season_id)

    if request.method == "POST":
        name = season.name
        season.delete()
        messages.success(request, f"Season '{name}' deleted.")
        return redirect("registration_season_list")

    return render(request, "registration/seasons/delete_confirm.html", {"season": season, "subnav_active": "seasons"})


@require_permission("gold_team")
def event_list(request: HttpRequest, season_id: int) -> HttpResponse:
    season = get_object_or_404(Season, id=season_id)
    events = season.events.annotate_enrollment_count()

    return render(
        request, "registration/events/list.html", {"season": season, "events": events, "subnav_active": "seasons"}
    )


@require_permission("gold_team")
def event_create(request: HttpRequest, season_id: int) -> HttpResponse:
    season = get_object_or_404(Season, id=season_id)

    if request.method == "POST":
        form = EventForm(request.POST)
        if form.is_valid():
            event = form.save(commit=False)
            event.season = season
            event.save()
            messages.success(request, f"Event '{event.name}' created successfully.")
            return redirect("registration_event_list", season_id=season.id)
    else:
        form = EventForm()

    return render(
        request,
        "registration/events/form.html",
        {"form": form, "season": season, "title": "Create Event", "subnav_active": "seasons"},
    )


@require_permission("gold_team")
def event_edit(request: HttpRequest, event_id: int) -> HttpResponse:
    event = get_object_or_404(Event, id=event_id)

    if request.method == "POST":
        form = EventForm(request.POST, instance=event)
        if form.is_valid():
            # Only the form's fields: is_active is written by Event.open() alone
            form.save(commit=False).save(update_fields=[*EventForm.Meta.fields, "updated_at"])
            messages.success(request, f"Event '{event.name}' updated successfully.")
            return redirect("registration_event_list", season_id=event.season_id)
    else:
        form = EventForm(instance=event)

    return render(
        request,
        "registration/events/form.html",
        {"form": form, "season": event.season, "event": event, "title": "Edit Event", "subnav_active": "seasons"},
    )


@require_permission("gold_team")
def event_delete(request: HttpRequest, event_id: int) -> HttpResponse:
    event = get_object_or_404(Event, id=event_id)
    season_id = event.season_id

    if request.method == "POST":
        name = event.name
        event.delete()
        messages.success(request, f"Event '{name}' deleted.")
        return redirect("registration_event_list", season_id=season_id)

    return render(request, "registration/events/delete_confirm.html", {"event": event, "subnav_active": "seasons"})


@require_permission("gold_team")
def event_detail(request: HttpRequest, event_id: int) -> HttpResponse:
    """View event details with enrolled registrations and team assignments (Gold Team only)."""
    event = get_object_or_404(Event, id=event_id)
    enrollments = (
        RegistrationEventEnrollment.objects.filter(event=event)
        .select_related("registration")
        .prefetch_related("registration__contacts")
    )
    assignments = list(EventTeamAssignment.objects.filter(event=event).select_related("registration", "team", "school"))

    # Imported schools have no registration: match those by school
    by_registration = {a.registration_id: a for a in assignments if a.registration_id}
    by_school = {a.school.name_key: a for a in assignments}

    enrollment_data = []
    for enrollment in enrollments:
        assignment = by_registration.get(enrollment.registration_id) or by_school.get(
            normalize_school_name(enrollment.registration.school_name)
        )
        enrollment_data.append(
            {
                "enrollment": enrollment,
                "registration": enrollment.registration,
                "assignment": assignment,
                "can_assign": enrollment.registration.status == "paid" and not assignment,
            }
        )

    sent = packets_sent(event)
    context = {
        "event": event,
        "enrollment_data": enrollment_data,
        "total_enrolled": enrollments.count(),
        "total_assigned": len(assignments),
        "assignable_count": sum(1 for e in enrollment_data if e["can_assign"]),
        "packets_sent": sent,
        # Only the open event's teams can be unassigned, and only until packets go out
        "can_unassign": event.is_active and not sent,
        "subnav_active": "seasons",
    }

    return render(request, "registration/events/detail.html", context)


@require_permission("gold_team")
def assign_teams(request: HttpRequest, event_id: int) -> HttpResponse:
    """Randomly assign team numbers to all eligible registrations for an event (Gold Team only)."""
    if request.method != "POST":
        return redirect("registration_event_detail", event_id=event_id)

    event = get_object_or_404(Event, id=event_id)
    if event != Event.objects.open():
        messages.error(request, "Teams can only be assigned for the open event: import its school list first.")
        return redirect("registration_event_detail", event_id=event_id)

    enrolled_registrations = (
        RegistrationEventEnrollment.objects.filter(
            event=event,
            registration__status="paid",
        )
        .exclude(registration__team_assignments__event=event)
        .select_related("registration")
        .prefetch_related("registration__contacts")
        .order_by("enrolled_at", "pk")
    )

    # A school holds one team, whether it was imported or assigned here
    taken = set(SchoolInfo.objects.values_list("school__name_key", flat=True))
    taken |= set(EventTeamAssignment.objects.filter(event=event).values_list("school__name_key", flat=True))
    registrations_to_assign: list[tuple[TeamRegistration, str]] = []
    skipped = []
    no_contact = []
    team_taken: list[str] = []
    for enrollment in enrolled_registrations:
        registration = enrollment.registration
        key = normalize_school_name(registration.school_name)
        contact_email = _primary_contact_email(registration)
        if key in taken:
            skipped.append(registration.school_name)
        elif not contact_email:
            no_contact.append(registration.school_name)
        else:
            taken.add(key)
            registrations_to_assign.append((registration, contact_email))

    def warn_skipped() -> None:
        if skipped:
            messages.warning(request, f"Skipped {', '.join(skipped)}: that school already has a team.")
        if no_contact:
            messages.warning(request, f"Skipped {', '.join(no_contact)}: no contact email on the registration.")
        if team_taken:
            messages.warning(
                request,
                f"Skipped {', '.join(team_taken)}: the team picked for it got a school meanwhile. Assign again.",
            )

    if not registrations_to_assign:
        warn_skipped()
        messages.warning(request, "No eligible registrations to assign.")
        return redirect("registration_event_detail", event_id=event_id)

    assigned_team_ids = EventTeamAssignment.objects.filter(event=event).values_list("team_id", flat=True)
    available_teams = list(
        Team.objects.exclude(id__in=assigned_team_ids).filter(school_info__isnull=True).order_by("team_number")
    )

    if len(registrations_to_assign) > len(available_teams):
        warn_skipped()
        messages.error(
            request,
            f"Not enough teams available. Need {len(registrations_to_assign)}, have {len(available_teams)}.",
        )
        return redirect("registration_event_detail", event_id=event_id)

    random.shuffle(available_teams)

    assigned = 0
    with transaction.atomic():
        for team, (registration, contact_email) in zip(available_teams, registrations_to_assign, strict=False):
            # Each in its own savepoint: a school claimed meanwhile (another tab, an edit) skips just this one
            try:
                with transaction.atomic():
                    set_team_school(
                        team,
                        registration.school_name,
                        contact_email=contact_email,
                        updated_by=request.user.get_username(),
                        registration=registration,
                        expect_free=True,
                    )
                    activate_teams([team])
            except SchoolTakenError, IntegrityError:
                skipped.append(registration.school_name)
            except TeamTakenError:
                team_taken.append(registration.school_name)
            else:
                assigned += 1

    warn_skipped()
    if assigned:
        messages.success(request, f"Assigned {assigned} teams successfully.")
    return redirect("registration_event_detail", event_id=event_id)


def _primary_contact_email(registration: TeamRegistration) -> str:
    """The captain's email, else the first contact's; empty when the registration has none."""
    contacts = sorted(registration.contacts.all(), key=lambda contact: (contact.role != "captain", contact.pk))
    return contacts[0].email if contacts else ""


@require_permission("gold_team")
def unassign_team(request: HttpRequest, assignment_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("registration_season_list")

    assignment = get_object_or_404(EventTeamAssignment, id=assignment_id)
    event_id = assignment.event_id

    if assignment.event != Event.objects.open():
        messages.error(request, "Only the open event's teams can be unassigned; earlier events are history.")
        return redirect("registration_event_detail", event_id=event_id)

    school_name = assignment.school.name
    shown = ShownSchoolForm(request.POST)
    try:
        remove_team_school(assignment.team, expected_school_id=shown.expected_school_id())
    except (PacketsSentError, SchoolChangedError) as refused:
        messages.error(request, str(refused))
        return redirect("registration_event_detail", event_id=event_id)
    messages.success(request, f"Team assignment for {school_name} removed.")

    return redirect("registration_event_detail", event_id=event_id)
