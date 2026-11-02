import random
from enum import StrEnum
from typing import cast

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.forms import CheckboxSelectMultiple
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django_stubs_ext import WithAnnotations
from packets.models import packets_sent

from core.auth_utils import require_permission
from core.form_errors import fields_with_errors
from core.form_renderer import PortalFormRenderer
from core.utils import client_ip, list_filters, optional_int, requested_sort, stable_order
from team.models import SchoolInfo

from .forms import EventForm, RegistrationForm, RejectRegistrationForm, SeasonForm, ShownSchoolForm
from .locks import school_list_lock
from .models import (
    Event,
    EventTeamAssignment,
    RegistrationEventEnrollment,
    RegistrationOpen,
    RegistrationRateLimit,
    Season,
    TeamRegistration,
    normalize_school_name,
)
from .services import (
    EventKeptError,
    PacketsSentError,
    SchoolChangedError,
    SchoolTakenError,
    TeamTakenError,
    activate_teams,
    delete_event,
    delete_season,
    event_delete_problem,
    free_teams,
    remove_team_school,
    season_delete_problem,
    set_team_school,
)


class RegistrationPageStatus(StrEnum):
    FINALIZED = "finalized"
    ACTIVE = "active"
    REGISTRATION_OPEN = "registration_open"
    REGISTRATION_CLOSED = "registration_closed"


def registration_page_status(event: WithAnnotations[Event, RegistrationOpen]) -> RegistrationPageStatus:
    """The event's status on the event list and the event's page: the first of these that holds."""
    if event.is_finalized:
        return RegistrationPageStatus.FINALIZED
    if event.is_active:
        return RegistrationPageStatus.ACTIVE
    if event.registration_is_open:
        return RegistrationPageStatus.REGISTRATION_OPEN
    return RegistrationPageStatus.REGISTRATION_CLOSED


EVENT_LIST_STATUS_SHOWN: dict[RegistrationPageStatus, tuple[str, str]] = {
    RegistrationPageStatus.FINALIZED: ("Finalized", "neutral"),
    RegistrationPageStatus.ACTIVE: ("Active", "success"),
    RegistrationPageStatus.REGISTRATION_OPEN: ("Open", "info"),
    RegistrationPageStatus.REGISTRATION_CLOSED: ("Closed", "warning"),
}
EVENT_DETAIL_STATUS_SHOWN: dict[RegistrationPageStatus, tuple[str, str]] = {
    **EVENT_LIST_STATUS_SHOWN,
    RegistrationPageStatus.REGISTRATION_OPEN: ("Registration Open", "info"),
    RegistrationPageStatus.REGISTRATION_CLOSED: ("Registration Closed", "warning"),
}


class PublicPageRenderer(PortalFormRenderer):
    """Renders a field as a row of the standalone register page, in its own look."""

    field_template_name = "registration/public/field.html"


def public_registration_form(data: QueryDict | None = None) -> RegistrationForm:
    form = RegistrationForm(data, renderer=PublicPageRenderer())
    events = cast(CheckboxSelectMultiple, form.fields["events"].widget)
    events.template_name = "registration/public/event_cards.html"
    events.option_template_name = "registration/public/event_card.html"
    return form


def error_summary(form: RegistrationForm) -> list[tuple[str, str]]:
    """The register page's failed-submit summary: (link to the field, or "" for the form's own error, line)."""
    lines = [("", str(error)) for error in form.non_field_errors()]
    for field, label, anchor in fields_with_errors(form):
        # The events' and rules' own messages name what to do; their labels are whole sentences
        named = not (field.use_fieldset or field.widget_type == "checkbox")
        lines += [(f"#{anchor}", f"{label}: {error}" if named else str(error)) for error in field.errors]
    return lines


def register(request: HttpRequest) -> HttpResponse:
    """Public registration form (no authentication required)."""
    if request.method == "POST":
        ip = client_ip(request)
        with RegistrationRateLimit.held(ip) as over_limit:
            if over_limit:
                messages.error(request, "Too many registration submissions. Please try again later.")
                return render(request, "registration/register.html", {"form": public_registration_form()}, status=429)

            form = public_registration_form(request.POST)
            if form.is_valid():
                RegistrationRateLimit.record(ip)  # Only count successful submissions
                form.save()
                messages.success(request, "Registration submitted successfully!")
                return redirect("registration_register")
    else:
        form = public_registration_form()

    return render(request, "registration/register.html", {"form": form, "error_summary": error_summary(form)})


@require_permission("gold_team")
def review_list(request: HttpRequest) -> HttpResponse:
    filters, filtered = list_filters(request, {"status": "", "search": ""})

    registrations = TeamRegistration.objects.prefetch_related("contacts", "event_enrollments__event").all()
    if filters["status"]:
        registrations = registrations.filter(status=filters["status"])
    if search := filters["search"]:
        registrations = registrations.filter(
            Q(school_name__icontains=search)
            | Q(contacts__name__icontains=search)
            | Q(contacts__email__icontains=search)
        ).distinct()

    sort_by = requested_sort(request, ("school_name", "status", "submitted_at"), "-submitted_at")
    registrations = stable_order(registrations, sort_by)

    context = {
        "registrations": registrations,
        "filters": filters,
        "filtered": filtered,
        "sort_by": sort_by,
        "status_choices": TeamRegistration.STATUS_CHOICES,
    }

    if request.headers.get("HX-Request"):
        return render(request, "registration/review_table.html", context)

    return render(request, "registration/review_list.html", context)


@require_permission("gold_team")
@require_POST
def approve_registration(request: HttpRequest, registration_id: int) -> HttpResponse:
    registration = get_object_or_404(TeamRegistration, id=registration_id)
    if request.user.is_authenticated and registration.approve(request.user):
        messages.success(request, f"Registration for {registration.school_name} has been approved.")
    else:
        messages.error(request, f"Registration for {registration.school_name} is no longer pending.")

    return redirect("registration_review_list")


@require_permission("gold_team")
def reject_registration(request: HttpRequest, registration_id: int) -> HttpResponse:
    registration = get_object_or_404(TeamRegistration, id=registration_id)

    context = {"registration": registration}

    if request.method == "POST":
        form = RejectRegistrationForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Please provide a reason for rejection.")
            return render(request, "registration/reject_confirm.html", context)
        if registration.reject(form.cleaned_data["reason"]):
            messages.success(request, f"Registration for {registration.school_name} has been rejected.")
        else:
            messages.error(request, f"Registration for {registration.school_name} is no longer pending.")
        return redirect("registration_review_list")

    return render(request, "registration/reject_confirm.html", context)


@require_permission("gold_team")
def season_list(request: HttpRequest) -> HttpResponse:
    filters, filtered = list_filters(request, {"status": "", "search": ""})
    seasons = Season.objects.annotate(event_count=Count("events"))

    if filters["status"] == "active":
        seasons = seasons.filter(is_active=True)
    elif filters["status"] == "inactive":
        seasons = seasons.filter(is_active=False)

    if search := filters["search"]:
        query = Q(name__icontains=search)
        if (year := optional_int(search)) is not None:
            query |= Q(year=year)
        seasons = seasons.filter(query)

    sort_by = requested_sort(request, ("name", "year", "event_count"), "-year")
    seasons = stable_order(seasons, sort_by)

    context = {
        "seasons": seasons,
        "filters": filters,
        "filtered": filtered,
        "sort_by": sort_by,
    }

    if request.headers.get("HX-Request"):
        return render(request, "registration/seasons/table.html", context)

    return render(request, "registration/seasons/list.html", context)


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

    return render(request, "registration/seasons/form.html", {"form": form, "title": "Create Season"})


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
        {"form": form, "title": "Edit Season"},
    )


@require_permission("gold_team")
def season_delete(request: HttpRequest, season_id: int) -> HttpResponse:
    season = get_object_or_404(Season, id=season_id)

    if request.method == "POST":
        name = season.name
        try:
            delete_season(season)
        except EventKeptError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f"Season '{name}' deleted.")
        return redirect("registration_season_list")

    return render(
        request,
        "registration/seasons/delete_confirm.html",
        {"season": season, "problem": season_delete_problem(season)},
    )


@require_permission("gold_team")
def event_list(request: HttpRequest, season_id: int) -> HttpResponse:
    season = get_object_or_404(Season, id=season_id)
    filters, filtered = list_filters(request, {"event_type": "", "search": ""})

    events_qs = season.events.with_registration_open().annotate(enrollment_count=Count("enrollments"))
    if filters["event_type"]:
        events_qs = events_qs.filter(event_type=filters["event_type"])
    if search := filters["search"]:
        events_qs = events_qs.filter(name__icontains=search)

    sort_by = requested_sort(
        request,
        ("name", "event_type", "date", "start_time", "enrollment_count"),
        "date",
    )
    events_qs = stable_order(events_qs, sort_by)

    events = [(event, *EVENT_LIST_STATUS_SHOWN[registration_page_status(event)]) for event in events_qs]

    context = {
        "season": season,
        "events": events,
        "filters": filters,
        "filtered": filtered,
        "sort_by": sort_by,
        "event_type_choices": Event.EVENT_TYPE_CHOICES,
    }

    if request.headers.get("HX-Request"):
        return render(request, "registration/events/table.html", context)

    return render(request, "registration/events/list.html", context)


@require_permission("gold_team")
def event_create(request: HttpRequest, season_id: int) -> HttpResponse:
    season = get_object_or_404(Season, id=season_id)

    if request.method == "POST":
        form = EventForm(request.POST, instance=Event(season=season))
        if form.is_valid():
            event = form.save()
            messages.success(request, f"Event '{event.name}' created successfully.")
            return redirect("registration_event_list", season_id=season.id)
    else:
        form = EventForm()

    return render(
        request,
        "registration/events/form.html",
        {"form": form, "season": season, "title": "Create Event"},
    )


@require_permission("gold_team")
def event_edit(request: HttpRequest, event_id: int) -> HttpResponse:
    event = get_object_or_404(Event, id=event_id)

    if request.method == "POST":
        form = EventForm(request.POST, instance=event)
        if form.is_valid():
            # Only the form's fields: is_active is written only by Event.open and Event.close
            form.save(commit=False).save(update_fields=[*EventForm.Meta.fields, "updated_at"])
            messages.success(request, f"Event '{event.name}' updated successfully.")
            return redirect("registration_event_list", season_id=event.season_id)
    else:
        form = EventForm(instance=event)

    return render(
        request,
        "registration/events/form.html",
        {"form": form, "season": event.season, "title": "Edit Event"},
    )


@require_permission("gold_team")
def event_delete(request: HttpRequest, event_id: int) -> HttpResponse:
    event = get_object_or_404(Event, id=event_id)
    season_id = event.season_id

    if request.method == "POST":
        name = event.name
        try:
            delete_event(event)
        except EventKeptError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f"Event '{name}' deleted.")
        return redirect("registration_event_list", season_id=season_id)

    return render(
        request,
        "registration/events/delete_confirm.html",
        {"event": event, "problem": event_delete_problem(event)},
    )


@require_permission("gold_team")
def event_detail(request: HttpRequest, event_id: int) -> HttpResponse:
    """View event details with enrolled registrations and team assignments (Gold Team only)."""
    event = get_object_or_404(Event.objects.with_registration_open(), id=event_id)
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
                "can_assign": enrollment.registration.status == "approved" and not assignment,
            }
        )

    sent = packets_sent(event)
    status_word, status_badge = EVENT_DETAIL_STATUS_SHOWN[registration_page_status(event)]
    context = {
        "event": event,
        "status_word": status_word,
        "status_badge": status_badge,
        "enrollment_data": enrollment_data,
        "total_enrolled": enrollments.count(),
        "total_assigned": len(assignments),
        "assignable_count": sum(1 for e in enrollment_data if e["can_assign"]),
        "packets_sent": sent,
        # Only the open event's teams can be unassigned, and only until packets go out
        "can_unassign": event.is_active and not sent,
    }

    return render(request, "registration/events/detail.html", context)


@require_permission("gold_team")
@require_POST
def assign_teams(request: HttpRequest, event_id: int) -> HttpResponse:
    """Randomly assign team numbers to all eligible registrations for an event (Gold Team only)."""
    skipped: list[str] = []
    no_contact: list[str] = []
    team_taken: list[str] = []

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

    with transaction.atomic():
        school_list_lock()
        event = get_object_or_404(Event, id=event_id)
        if event != Event.objects.open():
            messages.error(request, "Teams can only be assigned for the open event: import its school list first.")
            return redirect("registration_event_detail", event_id=event_id)

        enrolled_registrations = (
            RegistrationEventEnrollment.objects.filter(
                event=event,
                registration__status="approved",
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
        for enrollment in enrolled_registrations:
            registration = enrollment.registration
            key = normalize_school_name(registration.school_name)
            contact = registration.primary_contact
            contact_email = contact.email if contact else ""
            if key in taken:
                skipped.append(registration.school_name)
            elif not contact_email:
                no_contact.append(registration.school_name)
            else:
                taken.add(key)
                registrations_to_assign.append((registration, contact_email))

        if not registrations_to_assign:
            warn_skipped()
            messages.warning(request, "No eligible registrations to assign.")
            return redirect("registration_event_detail", event_id=event_id)

        available_teams = list(free_teams(event).order_by("team_number"))

        if len(registrations_to_assign) > len(available_teams):
            warn_skipped()
            messages.error(
                request,
                f"Not enough teams available. Need {len(registrations_to_assign)}, have {len(available_teams)}.",
            )
            return redirect("registration_event_detail", event_id=event_id)

        random.shuffle(available_teams)

        assigned = 0
        for team, (registration, contact_email) in zip(available_teams, registrations_to_assign, strict=False):
            # Each in its own savepoint: a school claimed meanwhile skips just this one
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


@require_permission("gold_team")
@require_POST
def unassign_team(request: HttpRequest, assignment_id: int) -> HttpResponse:
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
