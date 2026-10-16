"""Property tests for the packet views: a team's packets and downloads, and Gold Team's list, upload, detail,
distribution actions and resends. The properties are views_harness.check_answer's."""

import pytest
from hypothesis import strategies as st
from packets.forms import PacketActionForm, PacketResendForm, PacketUploadForm

from core.tests.views_harness import (
    ViewCase,
    World,
    any_request_is_answered_safely,
    echo_sweep,
    form_fields,
    may,
)

GOLD = may("gold_team")
TEAMS = may("blue_team")

CASES = [
    ViewCase("team_packet", TEAMS),
    ViewCase("download_packet", TEAMS),
    ViewCase("packets_list", GOLD),
    ViewCase("upload_packet", GOLD, post=lambda w: form_fields(PacketUploadForm())),
    ViewCase("packet_detail", GOLD),
    ViewCase(
        "packet_action",
        GOLD,
        post=lambda w: form_fields(
            PacketActionForm(),
            {
                "action": st.sampled_from(
                    ["distribute", "resend_failed", "resend_pending", "send_new_teams", "cancel", "test_email"]
                ),
                "team_id": st.sampled_from([str(t) for t in w.ids("team_id")]),
            },
        ),
    ),
    ViewCase("packet_resend_team", GOLD, post=lambda w: form_fields(PacketResendForm())),
]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_any_request_is_answered_safely(case: ViewCase, view_world: World) -> None:
    any_request_is_answered_safely(case, view_world)


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_every_field_is_escaped_where_echoed(case: ViewCase, view_world: World) -> None:
    echo_sweep(case, view_world)
