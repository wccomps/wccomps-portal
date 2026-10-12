import json
import logging
from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import close_old_connections, transaction
from django.template.loader import render_to_string
from registration.locks import school_list_lock
from registration.models import Event

from core.authentik_utils import reset_team_password
from core.utils import ndjson_done
from core.utils import ndjson_progress as _progress
from team.models import SchoolInfo, Team, team_username

from .models import Packet, PacketDistribution

logger = logging.getLogger(__name__)

NOT_A_DRAFT = "Only a draft packet can be distributed"
NO_OPEN_EVENT = "Import the school list for this event first"
EARLIER_EVENT_PACKET = "This packet belongs to an earlier event; team numbers have been reassigned since"
NO_EVENT_OPEN = "No event is open."
UNRECORDED_EVENT_PACKET = "This packet was sent before events were recorded; team numbers may have changed since"
NOT_SENT_YET = "Only a sent packet can be sent to teams that haven't received it; distribute this one."
EVERY_TEAM_HAS_IT = "Every team on the list already has this packet."


@transaction.atomic
def begin_distribution(packet: Packet) -> None:
    """Start sending a draft packet, making the open event its event; ValueError says why it can't start.

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


class PacketDistributionService:
    def distribute_packet(self, packet: Packet) -> dict[str, int]:
        """Distribute a packet to all teams."""
        begin_distribution(packet)

        distributions_created = self._create_distributions_for_teams(packet)

        email_stats = {"sent": 0, "failed": 0}
        if packet.send_via_email:
            email_stats = self._send_emails_for_packet(packet)

        if email_stats["failed"] == 0:
            packet.mark_as_completed()

        return {
            "total": Team.objects.filter(is_active=True).count(),
            "email_sent": email_stats["sent"],
            "email_failed": email_stats["failed"],
            "created": distributions_created,
        }

    def _create_distributions_for_teams(self, packet: Packet) -> int:
        """Create PacketDistribution records for active teams with contact emails."""
        teams = Team.objects.filter(is_active=True).exclude(school_info=None)

        existing_teams = PacketDistribution.objects.filter(packet=packet).values_list("team_id", flat=True)
        teams_to_create = teams.exclude(id__in=existing_teams)

        distributions = [
            PacketDistribution(
                packet=packet,
                team=team,
                web_access_enabled=packet.web_access_enabled,
            )
            for team in teams_to_create
        ]

        if distributions:
            PacketDistribution.objects.bulk_create(distributions)
            logger.info(f"Created {len(distributions)} distributions for packet {packet.id}")

        return len(distributions)

    def _send_emails_for_packet(self, packet: Packet) -> dict[str, int]:
        distributions = PacketDistribution.objects.filter(packet=packet, email_status="pending").select_related("team")

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

    def _team_password(self, team: Team) -> str:
        """The team account's password for this competition, setting a new one in Authentik on first use."""
        school_info = SchoolInfo.objects.filter(team=team).first()
        if school_info is None:
            raise ValueError(f"Team {team.team_number} has no school info; import the school list first")
        if school_info.password:
            return school_info.password

        password, error = reset_team_password(team.team_number)
        if password is None:
            raise ValueError(f"Failed to set Authentik password for team {team.team_number}: {error}")
        logger.info(f"Generated credentials for team {team.team_number}")
        return password

    def send_packet_email(self, distribution: PacketDistribution, override_emails: list[str] | None = None) -> None:
        """Send packet email to a team, or to override_emails instead of the SchoolInfo addresses."""
        packet = distribution.packet
        team = distribution.team

        if override_emails:
            recipients = override_emails
        else:
            email_address = self._get_team_email(team)
            if not email_address:
                raise ValueError(f"No email address for team {team.team_number}")
            recipients = [email_address]

        password = self._team_password(team)

        username = team_username(team.team_number)
        raw_extras = packet.team_extras.get(str(team.team_number), {}) if packet.team_extras else {}
        # Format keys for display: "api_key" -> "API Key", "max_spend_usd" -> "Max Spend USD"
        team_extras = {k.replace("_", " ").title(): v for k, v in raw_extras.items()}
        context = {
            "packet": packet,
            "team": team,
            "distribution": distribution,
            "username": username,
            "password": password,
            "team_extras": team_extras,
        }

        subject = f"WCComps: {packet.title}"
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
        password = self._team_password(team)

        username = team_username(team.team_number)
        raw_extras = packet.team_extras.get(str(team.team_number), {}) if packet.team_extras else {}
        team_extras = {k.replace("_", " ").title(): v for k, v in raw_extras.items()}
        context = {
            "packet": packet,
            "team": team,
            "username": username,
            "password": password,
            "team_extras": team_extras,
        }

        subject = f"[TEST] WCComps: {packet.title}"
        text_content = render_to_string("packets/emails/packet_notification.txt", context)
        html_content = render_to_string("packets/emails/packet_notification.html", context)

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[email],
            reply_to=[settings.DEFAULT_REPLY_TO_EMAIL],
        )
        msg.attach_alternative(html_content, "text/html")
        msg.attach(packet.filename, bytes(packet.file_data), packet.mime_type)
        msg.send(fail_silently=False)

        logger.info(f"Sent test email for packet {packet.id} (team {team.team_number}) to {email}")

    def _get_team_email(self, team: Team) -> str | None:
        try:
            school_info = SchoolInfo.objects.get(team=team)
            return school_info.contact_email
        except SchoolInfo.DoesNotExist:
            logger.warning(f"No SchoolInfo found for team {team.team_number}")
            return None

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

    def _stream_parallel_send(self, distributions: list[PacketDistribution]) -> Iterator[str]:
        """Send emails in parallel, yielding progress as each completes."""
        total = len(distributions)
        completed = 0

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(self._send_one, dist): dist for dist in distributions}
            for future in as_completed(futures):
                dist, success, error = future.result()
                completed += 1
                if success:
                    yield _progress(f"Sent to Team {dist.team.team_number}", completed, total)
                else:
                    yield _progress(f"Failed Team {dist.team.team_number}: {error}", completed, total, ok=False)

    def _stream_counted_send(self, distributions: list[PacketDistribution]) -> Generator[str, None, tuple[int, int]]:
        """_stream_parallel_send's lines, then (sent, failed)."""
        sent = 0
        failed = 0
        for line in self._stream_parallel_send(distributions):
            yield line
            if json.loads(line).get("ok", True):
                sent += 1
            else:
                failed += 1
        return sent, failed

    def stream_distribute_packet(self, packet: Packet) -> Iterator[str]:
        try:
            begin_distribution(packet)
        except ValueError as e:
            yield ndjson_done(str(e), success=False)
            return
        self._create_distributions_for_teams(packet)

        distributions = list(
            PacketDistribution.objects.filter(packet=packet, email_status="pending").select_related("team")
        )
        total = len(distributions)

        if total == 0:
            packet.mark_as_completed()
            yield ndjson_done("No teams to distribute to")
            return

        sent, failed = yield from self._stream_counted_send(distributions)
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

        sent, still_failed = yield from self._stream_counted_send(dists)
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

        ValueError says why not: the packet wasn't sent yet, or check_resendable's refusal. Under the school list
        lock, so the list can't change between the check and the new distributions."""
        packet.refresh_from_db(fields=["status"])
        if packet.status not in ("distributing", "completed"):
            raise ValueError(NOT_SENT_YET)
        if refusal := check_resendable(packet):
            raise ValueError(refusal)
        had_it = set(PacketDistribution.objects.filter(packet=packet).values_list("team_id", flat=True))
        self._create_distributions_for_teams(packet)
        return list(PacketDistribution.objects.filter(packet=packet).exclude(team_id__in=had_it).select_related("team"))

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
        sent, failed = yield from self._stream_counted_send(distributions)
        yield ndjson_done(f"Sent {sent}, failed {failed}" if failed else f"Sent to {sent} new team(s)", failed == 0)
