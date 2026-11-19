"""Team validation and saving, a team's member count, and a Discord user's links."""

from datetime import timedelta
from itertools import count

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone

from core.models import CompetitionConfig
from team.models import MAX_TEAMS, DiscordLink, LinkAttempt, Team

pytestmark = pytest.mark.django_db

_users = count(1)


def _link(discord_id: int, team: Team | None = None, *, active: bool = True) -> DiscordLink:
    user = User.objects.create_user(username=f"member{next(_users)}")
    return DiscordLink.objects.create(
        discord_id=discord_id, discord_username=f"m{discord_id}", user=user, team=team, is_active=active
    )


def test_the_annotated_member_count_leaves_out_unlinked_members(django_assert_num_queries) -> None:
    """The team list counts only active links, as an unannotated team does, so an unlinked member frees a seat; the
    list reads the count from its own query rather than one query per team."""
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"max_team_members": 2})
    team = Team.objects.create(team_number=1, team_name="Team 01")
    Team.objects.create(team_number=2, team_name="Team 02")
    _link(101, team)
    _link(102, team, active=False)
    _link(103, team, active=False)

    counted = Team.objects.with_active_member_count().get(pk=team.pk)

    with django_assert_num_queries(0):
        assert counted.get_member_count() == 1
    assert Team.objects.get(pk=team.pk).get_member_count() == 1
    assert not counted.is_full()
    assert Team.objects.with_active_member_count().get(team_number=2).get_member_count() == 0


def test_relinking_ends_only_that_discord_users_active_link() -> None:
    """The user's active link ends now; another user's link and the user's earlier ended link are left as they were."""
    team = Team.objects.create(team_number=1, team_name="Team 01")
    current = _link(201, team)
    other = _link(202, team)
    ended_at = timezone.now() - timedelta(days=3)
    earlier = _link(201, active=False)
    DiscordLink.objects.filter(pk=earlier.pk).update(unlinked_at=ended_at)
    before = timezone.now()

    assert DiscordLink.deactivate_previous_links(201) == 1

    current.refresh_from_db()
    other.refresh_from_db()
    earlier.refresh_from_db()
    assert not current.is_active
    assert current.unlinked_at is not None and current.unlinked_at >= before
    assert other.is_active and other.unlinked_at is None
    assert earlier.unlinked_at == ended_at


@pytest.mark.parametrize("number", [0, MAX_TEAMS + 1])
def test_a_team_number_out_of_range_is_refused_on_its_field(number: int) -> None:
    with pytest.raises(ValidationError) as refused:
        Team.objects.create(team_number=number, team_name="Out of range")

    assert refused.value.message_dict == {
        "team_number": [f"Team number must be between 1 and {MAX_TEAMS}, got {number}"]
    }


def test_saving_an_existing_team_updates_it() -> None:
    team = Team.objects.create(team_number=4, team_name="Team 04")
    team.team_name = "Renamed"

    team.save()

    assert Team.objects.get(team_number=4).team_name == "Renamed"


def test_a_save_of_named_fields_keeps_a_change_made_meanwhile() -> None:
    """The ticket counter is saved alone, so a rename saved meanwhile from another copy of the row survives."""
    team = Team.objects.create(team_number=5, team_name="Team 05")
    Team.objects.filter(pk=team.pk).update(team_name="Renamed meanwhile")
    team.ticket_counter = 7

    team.save(update_fields=["ticket_counter"])

    saved = Team.objects.get(pk=team.pk)
    assert (saved.team_name, saved.ticket_counter) == ("Renamed meanwhile", 7)


@pytest.mark.parametrize(("success", "status"), [(True, "Success"), (False, "Failed")])
def test_a_link_attempt_names_who_tried_and_how_it_went(success: bool, status: str) -> None:
    """What the admin's link attempt pages and its change history call the attempt."""
    attempt = LinkAttempt(discord_id=1, discord_username="discorduser", authentik_username="team05", success=success)

    assert str(attempt) == f"discorduser to team05 ({status})"
