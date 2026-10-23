import logging
from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import close_old_connections, transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.template.loader import render_to_string
from django.urls import reverse
from registration.locks import school_list_lock
from registration.models import Event
from registration.stage import ACTIONS, Action

from core.authentik_utils import reset_team_password
from core.utils import ndjson_done
from core.utils import ndjson_progress as _progress
from team.models import SchoolInfo, Team, team_username

from .models import Packet, PacketDistribution

logger = logging.getLogger(__name__)

NOT_A_DRAFT = "Only a draft packet can be distributed"
NO_OPEN_EVENT = f"No event is open. {ACTIONS[Action.IMPORT].instruction} Then send the packet."
EARLIER_EVENT_PACKET = (
    "This packet belongs to an earlier event and team numbers have been reassigned since: upload it again as a new "
    "packet for the open event."
)
NO_EVENT_OPEN = (
    "No event is open, so team numbers may have changed since this packet was sent. Upload the packet again once the "
    "next school list is imported."
)
UNRECORDED_EVENT_PACKET = (
    "This packet was sent before events were recorded and team numbers may have changed since: upload it again as a "
    "new packet for the open event."
)
NOT_SENT_YET = "Only a sent packet can be sent to new teams: send it with Distribute first."
EVERY_TEAM_HAS_IT = (
    "No team on the list is without a copy of this packet; teams whose send failed or is pending are not included "
    "(use Resend Failed / Retry Pending)."
)


def packet_recipients() -> QuerySet[Team]:
    """The teams a packet send reaches: the active teams on the school list."""
    return Team.objects.filter(is_active=True).exclude(school_info=None)


def teams_without_delivered_copy(packets: QuerySet[Packet]) -> QuerySet[Team]:
    """The recipients missing a delivered copy of any of the packets: no distribution, or an emailed one whose email
    failed or is still pending (a packet not sent by email is delivered once its distribution exists)."""
    delivered = PacketDistribution.objects.filter(packet=OuterRef("pk"), team=OuterRef(OuterRef("pk"))).filter(
        Q(packet__send_via_email=False) | Q(email_status__in=PacketDistribution.DONE_STATUSES)
    )
    return packet_recipients().filter(Exists(packets.exclude(Exists(delivered))))


def _create_distributions_for_teams(packet: Packet) -> list[PacketDistribution]:
    """Create the packet's distributions for the recipients that have none, and return them.

    Callers hold the school list lock, so the list can't change between choosing the teams and creating the rows."""
    teams = packet_recipients().exclude(id__in=PacketDistribution.objects.filter(packet=packet).values("team_id"))
    distributions = PacketDistribution.objects.bulk_create(
        PacketDistribution(packet=packet, team=team, web_access_enabled=packet.web_access_enabled) for team in teams
    )
    if distributions:
        logger.info(f"Created {len(distributions)} distributions for packet {packet.id}")
    return distributions


@transaction.atomic
def begin_distribution(packet: Packet) -> list[PacketDistribution]:
    """Start sending a draft packet, making the open event its event, and return the distributions it created for
    the teams on the school list; ValueError says why it can't start.

    Under the school list lock, released when this returns and before any email goes out, so a send can't start
    while the school list or the open event is changing.
    """
    school_list_lock()
    packet.refresh_from_db(fields=["status"])
    if not packet.is_ready_for_distribution():
        raise ValueError(NOT_A_DRAFT)
    event = Event.objects.open()
    if event is None:
        raise ValueError(NO_OPEN_EVENT)
    if packet.event_id != event.pk:
        packet.event = event
        packet.save(update_fields=["event", "updated_at"])
    packet.mark_as_distributing()
    return _create_distributions_for_teams(packet)


@transaction.atomic
def check_resendable(packet: Packet) -> str | None:
    """Why the packet can't be sent again, or None. Team numbers are reshuffled between events, so a resend is only
    safe while the packet's own event is open: refused when no event is open, when the packet has no event, or when
    its event is an earlier one.

    Checked under the school list lock, so it doesn't read an event an import is opening. The lock goes with the
    caller's transaction: callers send their emails after it, outside the lock."""
    school_list_lock()
    event = Event.objects.open()
    if event is None:
        return NO_EVENT_OPEN
    if packet.event_id is None:
        return UNRECORDED_EVENT_PACKET
    if packet.event_id != event.pk:
        return EARLIER_EVENT_PACKET
    return None


def team_packet_url() -> str:
    """The absolute address of the team packet page, for emails."""
    return settings.BASE_URL.rstrip("/") + reverse("team_packet")


class PacketDistributionService:
    def distribute_packet(self, packet: Packet) -> dict[str, int]:
        """Distribute a packet to all teams."""
        distributions = begin_distribution(packet)

        email_stats = {"sent": 0, "failed": 0}
        if packet.send_via_email:
            email_stats = self._send_emails(distributions)

        if email_stats["failed"] == 0:
            packet.mark_as_completed()

        return {
            "total": Team.objects.filter(is_active=True).count(),
            "email_sent": email_stats["sent"],
            "email_failed": email_stats["failed"],
            "created": len(distributions),
        }

    def _send_emails(self, distributions: list[PacketDistribution]) -> dict[str, int]:
        sent_count = 0
        failed_count = 0

        for dist in distributions:
            try:
                self.send_packet_email(dist)
                sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send packet email to team {dist.team.team_number}: {e}")
                dist.mark_as_failed(str(e))
                failed_count += 1

        return {"sent": sent_count, "failed": failed_count}

    @transaction.atomic
    def _team_password(self, team: Team) -> str:
        """The team account's password for this competition, setting a new one in Authentik on first use.

        The team's school info row stays locked until the new password is stored on it, so concurrent sends set it
        once and all carry it."""
        school_info = SchoolInfo.objects.select_for_update().filter(team=team).first()
        if school_info is None:
            raise ValueError(f"Team {team.team_number} has no school info; import the school list first")
        if school_info.password:
            return school_info.password

        password, error = reset_team_password(team.team_number)
        if password is None:
            raise ValueError(f"Failed to set Authentik password for team {team.team_number}: {error}")
        logger.info(f"Generated credentials for team {team.team_number}")
        return password

    def _build_packet_email(
        self,
        packet: Packet,
        team: Team,
        recipients: list[str],
        subject: str,
        distribution: PacketDistribution | None = None,
    ) -> EmailMultiAlternatives:
        password = self._team_password(team)
        username = team_username(team.team_number)
        raw_extras = packet.team_extras.get(str(team.team_number), {}) if packet.team_extras else {}
        # Format keys for display: "api_key" -> "API Key", "max_spend_usd" -> "Max Spend USD"
        team_extras = {k.replace("_", " ").title(): v for k, v in raw_extras.items()}
        context: dict[str, object] = {
            "packet": packet,
            "team": team,
            "username": username,
            "password": password,
            "team_extras": team_extras,
            "portal_url": team_packet_url(),
        }
        if distribution is not None:
            context["distribution"] = distribution

        text_content = render_to_string("packets/emails/packet_notification.txt", context)
        html_content = render_to_string("packets/emails/packet_notification.html", context)

        email = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipients,
            reply_to=[settings.DEFAULT_REPLY_TO_EMAIL],
        )
        email.attach_alternative(html_content, "text/html")
        email.attach(packet.filename, bytes(packet.file_data), packet.mime_type)
        return email

    def send_packet_email(self, distribution: PacketDistribution, override_emails: list[str] | None = None) -> None:
        """Send packet email to a team, or to override_emails instead of the SchoolInfo addresses."""
        packet = distribution.packet
        team = distribution.team

        recipients = override_emails or team.school_emails
        if not recipients:
            raise ValueError(f"No email address for team {team.team_number}")

        email = self._build_packet_email(
            packet,
            team,
            recipients,
            f"WCComps: {packet.title}",
            distribution=distribution,
        )
        email.send(fail_silently=False)

        sent_to = ", ".join(recipients)
        distribution.mark_as_sent(sent_to)
        packet.complete_if_done()
        logger.info(f"Sent packet {packet.id} to team {team.team_number} at {sent_to}")

    def resend_to_team(self, packet: Packet, team: Team, primary_email: str, secondary_email: str = "") -> str | None:
        """Send the packet to one team again at these addresses, saving them as the team's contacts.

        Returns why not when the packet can't be resent or the team has no distribution or school on file;
        send failures raise.
        """
        # The check and the contact write under the school list lock; the email goes out after it is released
        with transaction.atomic():
            if refusal := check_resendable(packet):
                return refusal
            dist = PacketDistribution.objects.filter(packet=packet, team=team).first()
            if dist is None:
                return f"No distribution record for Team {team.team_number}."

            school_info = SchoolInfo.objects.filter(team=team).first()
            if school_info is None:
                return f"Team {team.team_number} has no school on file. Add it on the School Info page first."
            school_info.contact_email = primary_email
            school_info.secondary_email = secondary_email
            school_info.save(update_fields=["contact_email", "secondary_email"])

            dist.email_status = "pending"
            dist.email_error_message = ""
            dist.save(update_fields=["email_status", "email_error_message"])
        self.send_packet_email(dist, override_emails=[e for e in (primary_email, secondary_email) if e])
        return None

    def send_test_packet_email(self, packet: Packet, team: Team, email: str) -> None:
        """Send a test packet email to a specific address without creating distribution records."""
        msg = self._build_packet_email(packet, team, [email], f"[TEST] WCComps: {packet.title}")
        msg.send(fail_silently=False)

        logger.info(f"Sent test email for packet {packet.id} (team {team.team_number}) to {email}")

    def _send_one(self, dist: PacketDistribution) -> tuple[PacketDistribution, bool, str]:
        """Send a single packet email (thread-safe). Returns (dist, success, error)."""
        try:
            self.send_packet_email(dist)
            return dist, True, ""
        except Exception as e:
            logger.error(f"Failed to send packet email to team {dist.team.team_number}: {e}")
            dist.mark_as_failed(str(e))
            return dist, False, str(e)
        finally:
            close_old_connections()

    def _stream_parallel_send(self, distributions: list[PacketDistribution]) -> Generator[str, None, tuple[int, int]]:
        """Send emails in parallel, yielding progress as each completes; returns (sent, failed)."""
        total = len(distributions)
        sent = 0
        failed = 0

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(self._send_one, dist): dist for dist in distributions}
            for future in as_completed(futures):
                dist, success, error = future.result()
                if success:
                    sent += 1
                    yield _progress(f"Sent to Team {dist.team.team_number}", sent + failed, total)
                else:
                    failed += 1
                    yield _progress(f"Failed Team {dist.team.team_number}: {error}", sent + failed, total, ok=False)
        return sent, failed

    def stream_distribute_packet(self, packet: Packet) -> Iterator[str]:
        try:
            distributions = begin_distribution(packet)
        except ValueError as e:
            yield ndjson_done(str(e), success=False)
            return

        if not distributions:
            packet.mark_as_completed()
            yield ndjson_done("No teams to distribute to")
            return

        if not packet.send_via_email:
            packet.mark_as_completed()
            yield ndjson_done(f"Distributed to {len(distributions)} teams")
            return

        sent, failed = yield from self._stream_parallel_send(distributions)
        yield ndjson_done(f"Sent {sent}, failed {failed}" if failed else f"Distributed to {sent} teams", failed == 0)

    def _stream_resend(self, packet: Packet, status_filter: str, label: str) -> Iterator[str]:
        """Resend distributions matching a status filter, with parallel streaming progress."""
        if refusal := check_resendable(packet):
            yield ndjson_done(refusal, success=False)
            return
        dists = list(
            PacketDistribution.objects.filter(packet=packet, email_status=status_filter).select_related("team")
        )
        total = len(dists)

        if total == 0:
            yield ndjson_done(f"No {label} distributions to resend")
            return

        PacketDistribution.objects.filter(packet=packet, email_status=status_filter).update(
            email_status="pending", email_error_message=""
        )
        for dist in dists:
            dist.refresh_from_db()

        sent, still_failed = yield from self._stream_parallel_send(dists)
        yield ndjson_done(
            f"Resent {sent}, failed {still_failed}" if still_failed else f"Resent to {sent} teams", still_failed == 0
        )

    def stream_resend_failed(self, packet: Packet) -> Iterator[str]:
        yield from self._stream_resend(packet, "failed", "failed")

    def stream_retry_pending(self, packet: Packet) -> Iterator[str]:
        """Retry pending distributions (from interrupted sends) with streaming progress."""
        yield from self._stream_resend(packet, "pending", "pending")

    @transaction.atomic
    def _distributions_for_new_teams(self, packet: Packet) -> list[PacketDistribution]:
        """Create the packet's distributions for teams on the school list without one, and return them.

        ValueError says why not: the packet wasn't sent yet, or check_resendable's refusal. The status is read under
        the school list lock, so a first send of the packet has created its distributions by then, and the list can't
        change between the checks and the new distributions."""
        refusal = check_resendable(packet)  # Takes the lock, before the status is read
        packet.refresh_from_db(fields=["status"])
        if not packet.is_sent:
            raise ValueError(NOT_SENT_YET)
        if refusal:
            raise ValueError(refusal)
        return _create_distributions_for_teams(packet)

    def stream_send_to_new_teams(self, packet: Packet) -> Iterator[str]:
        """Send a sent packet to the teams added to the school list since, which have no distribution of it.

        Its event must still be the open one. The emails go out after the school list lock is released; the
        packet's status stays as it is."""
        try:
            distributions = self._distributions_for_new_teams(packet)
        except ValueError as refused:
            yield ndjson_done(str(refused), success=False)
            return
        if not distributions:
            yield ndjson_done(EVERY_TEAM_HAS_IT)
            return
        if not packet.send_via_email:
            packet.complete_if_done()
            sent, failed = len(distributions), 0
        else:
            sent, failed = yield from self._stream_parallel_send(distributions)
        message = f"Sent {sent}, failed {failed}" if failed else f"Sent to {sent} new team(s)"
        # Extras uploaded before these teams had their numbers have none for them
        numbers = sorted(dist.team.team_number for dist in distributions)
        if packet.team_extras and (no_extras := [n for n in numbers if str(n) not in packet.team_extras]):
            message += f". No per-team extras for {', '.join(f'Team {n}' for n in no_extras)}."
        yield ndjson_done(message, failed == 0)
