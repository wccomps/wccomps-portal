import re
from typing import TypedDict

from django import forms
from django.http import QueryDict

# A check's or criterion's points. Every check's maximum is summed into ScoringTemplate.orange_max, a
# numeric(10, 2) column, so no single value may come near its limit.
MAX_POINTS = 10_000
# A check's window and a follow-up reminder: at most a week ahead
MAX_MINUTES = 7 * 24 * 60


class OrangeCheckForm(forms.Form):
    title = forms.CharField(max_length=200)
    description = forms.CharField(required=False)
    scheduled_at = forms.DateTimeField(required=False)  # read in the viewer's timezone
    max_points = forms.IntegerField(min_value=0, max_value=MAX_POINTS, required=False)
    time_limit_minutes = forms.IntegerField(required=False, min_value=1, max_value=MAX_MINUTES)


class FollowUpForm(forms.Form):
    assignment_id = forms.IntegerField()
    minutes = forms.IntegerField(initial=15, min_value=1, max_value=MAX_MINUTES)
    note = forms.CharField(required=False)


class AssignmentRejectForm(forms.Form):
    notes = forms.CharField(required=False)


class ReassignForm(forms.Form):
    user_id = forms.IntegerField()


class SubmitScoreForm(forms.Form):
    score = forms.IntegerField(required=False, min_value=0)


ROW_NUMBER = re.compile(r"[0-9]{1,9}")


class CriterionInput(TypedDict):
    id: int | None
    label: str
    points: int
    sort_order: int


def extract_criteria(post_data: QueryDict) -> list[CriterionInput]:
    """Parse criterion_label_{n} / criterion_points_{n} / criterion_id_{n} rows in n order.

    n can skip values: the form keeps a row's number when an earlier row is removed.
    Labels are cut to 200 characters (OrangeCheckCriterion.label); rows without a label
    or a point value from 1 to MAX_POINTS are dropped. Numbers are ASCII digits: str.isdigit
    also accepts characters int() refuses, such as a superscript two.
    """
    numbers = sorted(int(m.group(1)) for key in post_data if (m := re.fullmatch(r"criterion_label_(\d+)", key)))
    criteria: list[CriterionInput] = []
    for n in numbers:
        label = post_data.get(f"criterion_label_{n}", "").strip()[:200]
        points_str = post_data.get(f"criterion_points_{n}", "").strip()
        id_str = post_data.get(f"criterion_id_{n}", "").strip()
        if not label or not ROW_NUMBER.fullmatch(points_str) or not 1 <= int(points_str) <= MAX_POINTS:
            continue
        criteria.append(
            {
                "id": int(id_str) if ROW_NUMBER.fullmatch(id_str) else None,
                "label": label,
                "points": int(points_str),
                "sort_order": len(criteria),
            }
        )
    return criteria
