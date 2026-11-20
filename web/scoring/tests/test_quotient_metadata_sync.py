"""Metadata sync: always a fresh fetch, and a failed one keeps the last good metadata."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from scoring.models import QuotientMetadataCache, ScoringTemplate
from scoring.quotient_sync import sync_quotient_metadata

pytestmark = pytest.mark.django_db


def test_a_failed_sync_keeps_the_last_metadata():
    """One Quotient blip mid-competition used to empty the red-team and incident dropdowns for 15 minutes."""
    QuotientMetadataCache.objects.create(boxes=[{"name": "web", "ip": "10.0.0.5"}], services=[{"name": "web:http"}])

    with patch("scoring.quotient_sync.QuotientClient") as client:
        client.return_value.get_infrastructure.return_value = None
        with pytest.raises(ValueError, match="Failed to retrieve infrastructure"):
            sync_quotient_metadata()

    client.return_value.get_infrastructure.assert_called_once_with(force_refresh=True)
    assert QuotientMetadataCache.objects.get().boxes == [{"name": "web", "ip": "10.0.0.5"}]


def test_metadata_the_database_cannot_store_is_reported_and_the_last_metadata_kept():
    from quotient.client import QuotientBox, QuotientInfrastructure

    QuotientMetadataCache.objects.create(boxes=[{"name": "web", "ip": "10.0.0.5", "services": []}])

    with patch("scoring.quotient_sync.QuotientClient") as client:
        client.return_value.get_infrastructure.return_value = QuotientInfrastructure(
            boxes=[QuotientBox(name="web\x00", ip="10.0.0.5", services=[])]
        )
        with pytest.raises(ValueError, match=r"^Quotient's metadata can't be stored: .+ holds a NUL character"):
            sync_quotient_metadata()

    assert QuotientMetadataCache.objects.get().boxes == [{"name": "web", "ip": "10.0.0.5", "services": []}]


def test_metadata_sync_derives_inject_max():
    from quotient.client import Inject

    ScoringTemplate.objects.create(
        service_weight=Decimal("40"), inject_weight=Decimal("40"), orange_weight=Decimal("20"), inject_max=Decimal("1")
    )
    infra = MagicMock(boxes=[])
    injects = [
        Inject(inject_id=1, title="Inject 00 (100 pts)", description=""),
        Inject(inject_id=2, title="Inject 01 (155 pts)", description=""),
    ]

    with patch("scoring.quotient_sync.QuotientClient") as client:
        client.return_value.get_infrastructure.return_value = infra
        client.return_value.get_injects.return_value = injects
        sync_quotient_metadata()

    assert ScoringTemplate.objects.first().inject_max == Decimal("255")


@pytest.fixture
def _empty_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.mark.usefixtures("_empty_cache")
def test_each_sync_reads_the_injects_afresh_not_from_the_page_cache(monkeypatch):
    """An inject's points changed in Quotient: the next sync, even within the cache's minute, uses them."""
    import httpx
    from quotient.client import QuotientClient

    ScoringTemplate.objects.create(
        service_weight=Decimal("40"), inject_weight=Decimal("40"), orange_weight=Decimal("20"), inject_max=Decimal("1")
    )
    answers = {
        "/api/metadata": {"boxes": []},
        "/api/injects": [{"ID": 1, "Title": "Inject 00 (100 pts)", "Description": ""}],
    }

    def request(self, method, path, **kwargs):
        return httpx.Response(
            200, json=answers[path], request=httpx.Request(method.upper(), f"https://quotient.test{path}")
        )

    monkeypatch.setattr(QuotientClient, "_request", request)
    with patch("core.proxmox.ProxmoxClient.list_team_vms", return_value=[]):
        sync_quotient_metadata()
        assert ScoringTemplate.objects.get().inject_max == Decimal("100")
        answers["/api/injects"] = [{"ID": 1, "Title": "Inject 00 (250 pts)", "Description": ""}]
        sync_quotient_metadata()

    assert ScoringTemplate.objects.get().inject_max == Decimal("250")


def _post_sync(admin_user, infrastructure):
    from django.test import Client
    from django.urls import reverse

    client = Client()
    client.force_login(admin_user)
    with patch("scoring.quotient_sync.QuotientClient") as quotient:
        quotient.return_value.get_infrastructure.return_value = infrastructure
        quotient.return_value.get_injects.return_value = []
        return client.post(reverse("admin_competition_action"), {"action": "sync_quotient"})


def test_a_sync_from_the_portal_records_who_ran_it(admin_user):
    from core.models import AuditLog

    response = _post_sync(admin_user, MagicMock(boxes=[]))

    assert response.status_code == 200, response.content
    assert QuotientMetadataCache.objects.get().synced_by == admin_user
    assert AuditLog.objects.get(action="quotient_metadata_synced").admin_user == admin_user.username


def test_an_unreachable_quotient_is_reported_not_answered_as_a_server_error(admin_user):
    """The request was fine: the page says why nothing synced."""
    from core.models import AuditLog

    response = _post_sync(admin_user, None)

    assert response.status_code == 200, response.content
    assert response.json() == {
        "success": False,
        "message": "Sync failed: Failed to retrieve infrastructure from Quotient",
    }
    assert not AuditLog.objects.filter(action="quotient_metadata_synced").exists()


def test_metadata_sync_merges_unscored_proxmox_team_vms_and_keeps_them_on_proxmox_blip():
    from quotient.client import QuotientBox, QuotientInfrastructure, QuotientService

    from core.proxmox import ProxmoxVM
    from scoring.quotient_sync import get_box_choices, get_service_choices

    infra = QuotientInfrastructure(
        boxes=[
            QuotientBox(
                name="TREX",
                ip="10.100.1_.12",
                services=[QuotientService(name="DNS", display_name="DNS")],
            )
        ]
    )
    proxmox_vms = [
        ProxmoxVM(vmid=10101, name="team01-trex", host="trex", node="pve01", status="running"),
        ProxmoxVM(vmid=10102, name="team01-pangea", host="pangea", node="pve01", status="running"),
    ]

    # With no teams, team 1 stands for every team's VMs
    with (
        patch("scoring.quotient_sync.QuotientClient") as client,
        patch("core.proxmox.ProxmoxClient.list_team_vms", side_effect=lambda number: {1: proxmox_vms}.get(number, [])),
    ):
        client.return_value.get_infrastructure.return_value = infra
        client.return_value.get_injects.return_value = []
        sync_quotient_metadata()

    cache = QuotientMetadataCache.objects.get()
    assert cache.boxes == [
        {"name": "TREX", "ip": "10.100.1_.12", "services": [{"name": "DNS", "display_name": "DNS"}]},
        {"name": "PANGEA", "ip": "", "services": []},
    ]
    assert get_box_choices() == [("TREX", ".12 TREX"), ("PANGEA", "PANGEA")]
    assert get_service_choices() == [("DNS", "DNS"), ("Other", "Other")]

    with (
        patch("scoring.quotient_sync.QuotientClient") as client,
        patch("core.proxmox.ProxmoxClient.list_team_vms", return_value=None),
    ):
        client.return_value.get_infrastructure.return_value = infra
        client.return_value.get_injects.return_value = []
        sync_quotient_metadata()

    assert QuotientMetadataCache.objects.get().boxes == cache.boxes

    with (
        patch("scoring.quotient_sync.QuotientClient") as client,
        patch("core.proxmox.ProxmoxClient.list_team_vms", return_value=[]),
    ):
        client.return_value.get_infrastructure.return_value = infra
        client.return_value.get_injects.return_value = []
        sync_quotient_metadata()

    assert QuotientMetadataCache.objects.get().boxes == cache.boxes


def _sync_with_proxmox(boxes, vms):
    from quotient.client import QuotientInfrastructure

    with (
        patch("scoring.quotient_sync.QuotientClient") as client,
        patch("core.proxmox.ProxmoxClient.list_team_vms", return_value=vms),
    ):
        client.return_value.get_infrastructure.return_value = QuotientInfrastructure(boxes=boxes)
        client.return_value.get_injects.return_value = []
        sync_quotient_metadata()
    return QuotientMetadataCache.objects.get().boxes


def test_a_proxmox_blip_keeps_only_the_unscored_boxes_quotient_does_not_list():
    """A box Quotient dropped (it had an IP or services), one it lists now, a duplicate or a nameless one goes."""
    from quotient.client import QuotientBox, QuotientService

    QuotientMetadataCache.objects.create(
        boxes=[
            {"name": "TREX", "ip": "", "services": []},
            {"name": "PANGEA", "ip": "", "services": []},
            {"name": "pangea", "ip": "", "services": []},
            {"name": "OLD", "ip": "10.0.0.9", "services": []},
            {"name": "GONE", "ip": "", "services": [{"name": "HTTP", "display_name": "HTTP"}]},
            {"ip": "", "services": []},
        ]
    )
    trex = QuotientBox(name="TREX", ip="10.0.0.12", services=[QuotientService(name="DNS", display_name="DNS")])

    boxes = _sync_with_proxmox([trex], None)

    assert boxes == [
        {"name": "TREX", "ip": "10.0.0.12", "services": [{"name": "DNS", "display_name": "DNS"}]},
        {"name": "PANGEA", "ip": "", "services": []},
    ]


def test_proxmox_vms_join_once_each_in_the_case_quotient_names_its_boxes():
    """A VM Quotient already lists by its bare host isn't added again, nor is a second VM with the same host."""
    from quotient.client import QuotientBox, QuotientService

    from core.proxmox import ProxmoxVM

    web = QuotientBox(name="web", ip="10.0.0.5", services=[QuotientService(name="HTTP", display_name="HTTP")])
    dc = QuotientBox(name="dc01.acme.local", ip="10.0.0.6", services=[])
    vms = [
        ProxmoxVM(vmid=10101, name="team01-dc01", host="dc01", node="pve01", status="running"),
        ProxmoxVM(vmid=10102, name="team01-router", host="router", node="pve01", status="running"),
        ProxmoxVM(vmid=10103, name="team01-router", host="router", node="pve02", status="stopped"),
        ProxmoxVM(vmid=10104, name="team01-web", host="web", node="pve01", status="running"),
    ]

    boxes = _sync_with_proxmox([web, dc], vms)

    assert [box["name"] for box in boxes] == ["web", "dc01.acme.local", "router"]


def test_proxmox_and_the_dns_check_sample_active_numbered_teams_with_a_school_first():
    """One sample team stands for every team's VMs and DNS answers, so it must be a real, registered team."""
    from django.test import override_settings
    from quotient.client import QuotientBox, QuotientInfrastructure, QuotientService
    from registration.models import School

    from core.proxmox import ProxmoxVM
    from team.models import MAX_TEAMS, SchoolInfo, Team

    def team(number, *, active=True, school=False):
        row = Team.objects.create(team_number=number, team_name=f"Team {number}", is_active=active)
        if school:
            SchoolInfo.objects.create(team=row, school=School.for_name(f"School {number}"), contact_email="c@x.edu")
        return row

    # Save refuses a number outside 1 to MAX_TEAMS; an update writes one anyway
    Team.objects.filter(pk=team(30, school=True).pk).update(team_number=0)
    Team.objects.filter(pk=team(31, school=True).pk).update(team_number=MAX_TEAMS + 1)
    team(2, active=False, school=True)
    team(1)
    team(MAX_TEAMS)
    team(9, school=True)
    team(4, school=True)
    infra = QuotientInfrastructure(
        boxes=[QuotientBox(name="BUGS", ip="10.100.1XX.10", services=[QuotientService(name="DNS", display_name="DNS")])]
    )
    vms = {4: [ProxmoxVM(vmid=10401, name="team04-gate", host="gate", node="pve01", status="running")]}

    with (
        override_settings(AWEFUL_PROXY_URL="http://proxy.example:8080"),
        patch("scoring.quotient_sync.QuotientClient") as client,
        patch("core.proxmox.ProxmoxClient.list_team_vms", side_effect=lambda number: vms.get(number, [])),
        patch("scoring.quotient_sync.httpx.post") as post,
    ):
        client.return_value.get_infrastructure.return_value = infra
        client.return_value.get_injects.return_value = []
        client.return_value.get_service_checks.return_value = []
        sync_quotient_metadata()

    # Team 4's VMs are the ones merged; the DNS check asks the first three sample teams, schools' teams first
    assert [box["name"] for box in QuotientMetadataCache.objects.get().boxes] == ["BUGS", "GATE"]
    assert [call.args[0] for call in client.return_value.get_service_checks.call_args_list] == [4, 9, 1]
    assert post.call_args.kwargs["json"]["number_of_teams"] == MAX_TEAMS


def test_metadata_sync_pushes_config_to_aweful_proxy():
    import httpx
    from django.test import override_settings
    from quotient.client import QuotientBox, QuotientInfrastructure, QuotientService, ServiceCheck

    from team.models import Team

    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    Team.objects.create(team_number=38, team_name="Team 38", is_active=True)

    infra = QuotientInfrastructure(
        boxes=[
            QuotientBox(
                name="BUGS",
                ip="10.100.1XX.10",
                services=[QuotientService(name="DNS", display_name="DNS")],
            )
        ]
    )
    checks = [
        ServiceCheck(
            round_number=1,
            timestamp=None,
            passed=False,
            debug=(
                "record 10.220.168.192.in-addr.arpa failed to get expected result "
                "within [bugs.acme.anvil], received [evil.spoofed.domain, 10.99.88.77]"
            ),
            error="incorrect answer",
        ),
        ServiceCheck(
            round_number=1,
            timestamp=None,
            passed=False,
            debug=(
                "record chat.acme.anvil failed to get expected result within [192.168.220.13], received [10.99.88.77]"
            ),
            error="incorrect answer",
        ),
    ]

    with (
        override_settings(AWEFUL_PROXY_URL="http://10.0.0.10:8080/", AWEFUL_PROXY_TOKEN="secret-token"),
        patch("scoring.quotient_sync.QuotientClient") as client,
        patch("scoring.quotient_sync.httpx.post") as mock_post,
    ):
        client.return_value.get_infrastructure.return_value = infra
        client.return_value.get_injects.return_value = []
        client.return_value.get_service_checks.return_value = checks
        sync_quotient_metadata()

    client.return_value.get_service_checks.assert_called_once_with(1, "BUGS:DNS", force_refresh=True)
    mock_post.assert_called_once()
    assert mock_post.call_args.args[0] == "http://10.0.0.10:8080/config"
    assert mock_post.call_args.kwargs["headers"] == {"Authorization": "Bearer secret-token"}
    assert mock_post.call_args.kwargs["json"] == {
        "number_of_teams": 38,
        "external_mask": "10.100.1xx.",
        "team_dns_server_last_octet": 10,
        "team_domain_name": "acme.anvil",
        "internal_mask": "192.168.220.",
    }

    with (
        override_settings(AWEFUL_PROXY_URL="http://10.0.0.10:8080"),
        patch("scoring.quotient_sync.QuotientClient") as client,
        patch("scoring.quotient_sync.httpx.post", side_effect=httpx.ConnectError("proxy down")),
    ):
        client.return_value.get_infrastructure.return_value = infra
        client.return_value.get_injects.return_value = []
        client.return_value.get_service_checks.return_value = checks
        sync_quotient_metadata()


# What a DNS check's debug line says: the team's domain, its internal network, or both
DOMAIN_ONLY = "record chat.acme.anvil failed to get expected result, received [10.99.88.77]"
NETWORK_ONLY = "record 10.220.168.192.in-addr.arpa failed to get expected result within [192.168.220.13]"
OTHER_BOTH = "record www.other.test failed to get expected result within [172.16.5.9]"


def _box(name, ip, *services):
    from quotient.client import QuotientBox, QuotientService

    return QuotientBox(name=name, ip=ip, services=[QuotientService(name=s, display_name=s) for s in services])


def _push_to_dns_proxy(boxes, answers=None, teams=()):
    """Sync with Quotient's DNS checks answering from answers[(team, "BOX:SERVICE")]; the proxy's post and the checks
    read, as (team, target)."""
    from django.test import override_settings
    from quotient.client import QuotientInfrastructure, ServiceCheck

    from team.models import Team

    for number in teams:
        Team.objects.create(team_number=number, team_name=f"Team {number}")

    def checks(team, target, force_refresh):
        debugs = (answers or {}).get((team, target), [])
        return [ServiceCheck(round_number=1, timestamp=None, passed=False, debug=d, error="") for d in debugs]

    with (
        override_settings(AWEFUL_PROXY_URL="http://proxy.example:8080", AWEFUL_PROXY_TOKEN=""),
        patch("scoring.quotient_sync.QuotientClient") as client,
        patch("core.proxmox.ProxmoxClient.list_team_vms", return_value=[]),
        patch("scoring.quotient_sync.httpx.post") as post,
    ):
        client.return_value.get_infrastructure.return_value = QuotientInfrastructure(boxes=boxes)
        client.return_value.get_injects.return_value = []
        client.return_value.get_service_checks.side_effect = checks
        sync_quotient_metadata()
    return post, [call.args[:2] for call in client.return_value.get_service_checks.call_args_list]


def test_the_dns_proxy_config_comes_from_the_first_dns_box_that_answers_it_all():
    """A box without DNS is passed over, a DNS box's other services aren't asked, and nothing after the answer is."""
    from django.conf import settings

    boxes = [
        _box("WEB", "10.100.1XX.5", "HTTP"),
        _box("BUGS", "10.100.1XX.10", "HTTP", "DNS"),
        _box("MAIL", "10.100.1XX.25", "DNS"),
    ]

    post, asked = _push_to_dns_proxy(boxes, {(1, "BUGS:DNS"): [DOMAIN_ONLY, NETWORK_ONLY]})

    assert asked == [(1, "BUGS:DNS")]
    assert post.call_args.kwargs == {
        "json": {
            "external_mask": "10.100.1xx.",
            "team_dns_server_last_octet": 10,
            "team_domain_name": "acme.anvil",
            "internal_mask": "192.168.220.",
        },
        "headers": None,
        "timeout": settings.HTTPX_DEFAULT_TIMEOUT,
    }


def test_a_dns_box_that_answers_part_of_the_config_leaves_the_rest_to_the_next():
    boxes = [_box("BUGS", "10.100.1XX.10", "DNS"), _box("NS2", "10.200.2XX.53", "DNS")]

    post, asked = _push_to_dns_proxy(boxes, {(1, "BUGS:DNS"): [NETWORK_ONLY], (1, "NS2:DNS"): [DOMAIN_ONLY]})

    assert asked == [(1, "BUGS:DNS"), (1, "NS2:DNS")]
    assert post.call_args.kwargs["json"] == {
        "external_mask": "10.100.1xx.",
        "team_dns_server_last_octet": 10,
        "internal_mask": "192.168.220.",
        "team_domain_name": "acme.anvil",
    }


@pytest.mark.parametrize(
    ("first_answer", "domain", "network"),
    [(DOMAIN_ONLY, "acme.anvil", "172.16.5."), (NETWORK_ONLY, "other.test", "192.168.220.")],
    ids=["domain-first", "network-first"],
)
def test_each_part_comes_from_the_first_sample_team_whose_check_shows_it(first_answer, domain, network):
    """Team 1's check shows one part; team 2's shows both, and only its missing part is taken. Team 3 isn't asked."""
    answers = {(1, "BUGS:DNS"): [first_answer], (2, "BUGS:DNS"): [OTHER_BOTH], (3, "BUGS:DNS"): [OTHER_BOTH]}

    post, asked = _push_to_dns_proxy([_box("BUGS", "10.100.1XX.10", "DNS")], answers, teams=(1, 2, 3))

    assert asked == [(1, "BUGS:DNS"), (2, "BUGS:DNS")]
    payload = post.call_args.kwargs["json"]
    assert (payload["team_domain_name"], payload["internal_mask"]) == (domain, network)


@pytest.mark.parametrize(
    ("ip", "external"),
    [
        ("10.100.1XX.1", {"external_mask": "10.100.1xx.", "team_dns_server_last_octet": 1}),
        (" 10.100.1_.254 ", {"external_mask": "10.100.1xx.", "team_dns_server_last_octet": 254}),
        ("10.100.1x.10", {"external_mask": "10.100.1xx.", "team_dns_server_last_octet": 10}),
        ("10.100.1XX.0", None),
        ("10.100.1XX.255", None),
        ("10.100.1XX", None),
        ("10.100.1XX.10.5", None),
        ("10.100.1XX.ten", None),
        ("10.100.101.10", None),
        ("10.1XX.1XX.10", None),
        ("10.300.1XX.10", None),
    ],
)
def test_the_dns_box_ip_gives_the_proxy_its_external_mask_only_with_one_team_placeholder(ip, external):
    """Team 1 stands in when there are no teams; a check that shows nothing leaves only the box's IP to push."""
    post, asked = _push_to_dns_proxy([_box("BUGS", ip, "DNS")])

    assert asked == [(1, "BUGS:DNS")]
    assert (post.call_args.kwargs["json"] if post.called else None) == external


@pytest.mark.parametrize(
    ("debugs", "domain"),
    [
        (["record 10.0.0.5 failed to get expected result", OTHER_BOTH], "other.test"),
        (["record acme.anvil failed to get expected result within [172.16.5.9]"], "acme.anvil"),
    ],
    ids=["an-ip-is-no-domain", "a-two-label-name-is-the-domain"],
)
def test_the_team_domain_is_a_checked_names_parent(debugs, domain):
    post, _ = _push_to_dns_proxy([_box("BUGS", "10.100.1XX.10", "DNS")], {(1, "BUGS:DNS"): debugs})

    assert post.call_args.kwargs["json"]["team_domain_name"] == domain
