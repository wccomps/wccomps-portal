"""Plain-text emails show values as typed; only their HTML parts escape them."""

from types import SimpleNamespace

from django.template.loader import render_to_string

RAW = "Horse-&123-<Battery>"


def test_packet_email_text_shows_the_password_as_typed():
    context = {
        "team": SimpleNamespace(team_number=7),
        "packet": SimpleNamespace(title="Packet & Rules"),
        "username": "team07",
        "password": RAW,
        "team_extras": {"API Key": RAW},
    }

    text = render_to_string("packets/emails/packet_notification.txt", context)
    html = render_to_string("packets/emails/packet_notification.html", context)

    assert f"Password: {RAW}" in text
    assert "Packet & Rules" in text
    assert "&amp;" not in text
    assert "Horse-&amp;123-&lt;Battery&gt;" in html


def test_scorecard_email_text_and_subject_show_names_as_typed():
    context = {"school_name": "A & B <College>", "team_number": 7}

    subject = render_to_string("emails/scorecard_subject.txt", context).strip()
    text = render_to_string("emails/scorecard.txt", context)

    assert subject == "WCComps Score Card - A & B <College>"
    assert "A & B <College>" in text
    assert "&amp;" not in text


def test_scorecard_email_has_no_blank_event_name():
    """Quotient never supplies an event name; the email used to read "participating in !" and "Card -  - "."""
    context = {"school_name": "Example University", "team_number": 7}

    subject = render_to_string("emails/scorecard_subject.txt", context).strip()
    text = render_to_string("emails/scorecard.txt", context)
    html = render_to_string("emails/scorecard.html", context)

    assert subject == "WCComps Score Card - Example University"
    for body in (text, html):
        assert "Thank you for participating!" in body
        assert " in !" not in body
    assert "Event:" not in text


def test_scorecard_email_includes_hardening_and_pcr_advisory():
    """Scorecard emails should include the hardening advisory and PCR reminder."""
    context = {"school_name": "Example University", "team_number": 7}

    text = render_to_string("emails/scorecard.txt", context)
    html = render_to_string("emails/scorecard.html", context)

    advisory = (
        "Be careful when hardening your systems because changing a single credential "
        "can silently break dependent services unless you update their configs too. "
        "Always report every updated password and rotated API token immediately via "
        "the Password Change Request (PCR) page on Quotient to keep scoring checks from failing. "
        "If your team gets stuck, do not let services sit red, be sure to request a consultation "
        "to get back on track instead of bleeding continuous scoring points."
    )
    assert advisory in text
    assert advisory in html
