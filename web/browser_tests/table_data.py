"""Rows for every data table, so pages render their tables (most show an empty state instead of an empty table)."""

from decimal import Decimal

from django.contrib.auth.models import User

from .conftest import make_test_data


def seed_table_data(viewer: User) -> dict:
    """make_test_data plus one row per table; some pages list only what belongs to `viewer`."""
    from django.utils import timezone
    from orange_team.models import OrangeAssignment, OrangeCheckIn, OrangeFollowUp
    from packets.models import Packet, PacketDistribution
    from registration.models import Event, School, Season
    from scoring.models import IncidentReport, InjectScore, OrangeTeamScore, RedTeamIPPool, RedTeamScore

    from team.models import DiscordLink, SchoolInfo
    from ticketing.models import Ticket, TicketHistory

    data = make_test_data()
    team, ticket = data["team"], data["ticket"]
    member = User.objects.create_user(username="table-member")
    now = timezone.now()

    DiscordLink.objects.create(user=member, discord_id=8800001, discord_username="member", team=team)
    SchoolInfo.objects.create(team=team, school=School.for_name("Example U"), contact_email="c@example.edu")
    TicketHistory.objects.create(ticket=ticket, action="created", actor=member, details={})
    Ticket.objects.create(
        ticket_number="TEAM01-002",
        title="Resolved ticket",
        team=team,
        category=ticket.category,
        status="resolved",
        resolved_at=now,
        points_charged=5,
    )

    # Scoring: an unapproved row for each review queue (with its select-all checkbox), and approved
    # inject points so the team has standings (leaderboard, scorecard, scorecard emails).
    pool = RedTeamIPPool.objects.create(name="Pool A", ip_addresses="10.0.0.1\n10.0.0.2", created_by=viewer)
    RedTeamScore.objects.filter(pk=data["finding"].pk).update(source_ip_pool=pool, submitted_by=member)
    IncidentReport.objects.filter(pk=data["incident"].pk).update(submitted_by=member)
    for inject_id, approved in (("1", False), ("2", True)):
        InjectScore.objects.create(
            team=team,
            inject_id=inject_id,
            inject_name=f"Inject {inject_id}",
            max_points=Decimal("100"),
            points_awarded=Decimal("80"),
            feedback="Good work",
            graded_by=member,
            is_approved=approved,
        )
    OrangeTeamScore.objects.create(team=team, submitted_by=member, description="Helpful", points_awarded=Decimal("5"))

    assignment = OrangeAssignment.objects.create(
        orange_check=data["orange_check"], user=member, team=team, status="submitted"
    )
    OrangeCheckIn.objects.create(user=member)
    OrangeFollowUp.objects.create(user=viewer, assignment=assignment, remind_at=now)

    # The packets list shows the draft/distributing packet on its own and the rest as history
    packet = data["packet"]
    packet.status = "distributing"
    packet.web_access_enabled = True
    # Teams see only the open event's packets
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    packet.event = Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=timezone.now().date(), is_active=True
    )
    packet.save()
    PacketDistribution.objects.create(packet=packet, team=team, web_access_enabled=True)
    Packet.objects.create(
        title="Earlier packet",
        file_data=b"%PDF-1.4",
        filename="earlier.pdf",
        mime_type="application/pdf",
        file_size=8,
        uploaded_by="browser-test",
        status="completed",
    )
    return data
