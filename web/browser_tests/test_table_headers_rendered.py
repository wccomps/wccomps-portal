"""Every data table's column titles line up, and a select-all checkbox sits over the row checkboxes.

The static check (core/tests/test_table_headers.py) keeps templates on the conventions that make this true;
this renders each page with rows in its tables and measures the result.
"""

from unittest.mock import patch

import pytest

from .conftest import _create_role_user, create_session_context, resolve_url
from .page_registry import PAGES
from .table_data import seed_table_data

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

# Where each header's text starts (the padded <a>/<span> c-table_header renders, else the cell), per table,
# and the horizontal centres of the select-all checkbox and the first row checkbox.
MEASURE = """() => [...document.querySelectorAll('table')].filter(t => t.offsetParent && t.tHead).map((table, i) => {
    const textTop = th => {
        const el = th.querySelector('.text a, .text span') || th.querySelector('.text') || th;
        return Math.round(el.getBoundingClientRect().top + parseFloat(getComputedStyle(el).paddingTop));
    };
    const headers = [...table.tHead.rows[0].cells];
    const titled = headers.filter(th => !th.querySelector('input[type=checkbox]') && th.textContent.trim());
    const centre = el => el && Math.round(el.getBoundingClientRect().left + el.getBoundingClientRect().width / 2);
    const rowBox = [...table.tBodies].flatMap(b => [...b.rows]).map(r => r.querySelector('input[type=checkbox]'))
        .find(Boolean);
    return {
        table: table.id || table.getAttribute('aria-label') || `table ${i + 1}`,
        rows: [...table.tBodies].reduce((n, b) => n + b.rows.length, 0),
        tops: titled.map(th => [th.textContent.trim().replace(/\\s+/g, ' '), textTop(th)]),
        headerBox: centre(table.tHead.querySelector('input[type=checkbox]')),
        rowBox: centre(rowBox),
    };
})"""


# Pages whose tables table_data.seed_table_data fills; a page missing here rendered no table, so it was never measured
PAGES_WITH_TABLES = {
    "ticket_list",
    "ticket_detail",
    "ops_review_tickets",
    "admin_teams",
    "admin_team_detail",
    "admin_categories",
    "school_info",
    "leaderboard_page",
    "scoring:red_team_scores",
    "scoring:red_team_findings",
    "scoring:ip_pool_list",
    "scoring:incident_list",
    "scoring:review_incidents",
    "scoring:review_orange",
    "scoring:inject_grades_review",
    "scoring:review_inject_feedback",
    "scoring:export_index",
    "scoring:email_scorecards",
    "scoring:scorecard",
    "scoring:inject_grading",
    "orange_team:dashboard",
    "orange_team:check_list",
    "orange_team:review_queue",
    "orange_team:check_detail",
    "team_packet",
    "packets_list",
    "packet_detail",
}


def _problems(page_name: str, tables: list[dict]) -> list[str]:
    problems = []
    for t in tables:
        where = f"{page_name} / {t['table']}"
        tops = [top for _, top in t["tops"]]
        if tops and max(tops) - min(tops) > 1:
            problems.append(f"{where}: titles start at different heights {t['tops']}")
        if t["headerBox"] is not None and t["rowBox"] is not None and abs(t["headerBox"] - t["rowBox"]) > 2:
            problems.append(f"{where}: select-all checkbox at x={t['headerBox']}, row checkbox at x={t['rowBox']}")
    return problems


def test_table_headers_line_up_on_every_page(live_server, pw_browser):
    from django.urls import reverse
    from quotient.client import Inject

    viewer = _create_role_user("admin", None)
    test_data = seed_table_data(viewer)
    urls = [
        (page_def.url_name, resolve_url(page_def, test_data))
        for page_def in PAGES
        if "admin" in page_def.allowed_roles and not page_def.expect_redirect
    ]
    # Not in PAGES: the role tests there would call the real Quotient
    urls.append(("scoring:inject_grading", reverse("scoring:inject_grading") + "?inject=1"))
    inject = Inject(
        inject_id=1,
        title="Inject 1",
        description="",
        open_time=None,
        due_time=None,
        close_time=None,
        files=[],
        submissions=[],
    )

    context = create_session_context(pw_browser, live_server, viewer)
    page = context.new_page()
    page.set_viewport_size({"width": 1400, "height": 900})
    problems: list[str] = []
    measured: set[str] = set()
    try:
        with patch("quotient.client.QuotientClient.get_injects", return_value=[inject]):
            for name, url in urls:
                page.goto(live_server.url + url, wait_until="networkidle")
                # Collapsed sections (e.g. ticket history) hide their tables until opened
                for toggle in page.query_selector_all('.fieldset-toggle-btn[aria-expanded="false"]'):
                    toggle.click()
                # x-collapse animates an opened section's height, ending at auto
                page.wait_for_function(
                    "() => [...document.querySelectorAll('[x-collapse]')]"
                    ".every(el => ['', 'auto'].includes(el.style.height))"
                )
                tables = page.evaluate(MEASURE)
                if any(t["rows"] for t in tables):
                    measured.add(name)
                problems += _problems(name, tables)
    finally:
        context.close()
    assert measured >= PAGES_WITH_TABLES, f"No table with rows on {sorted(PAGES_WITH_TABLES - measured)}"
    assert not problems, "\n".join(problems)
