import re
from typing import TypedDict

from django import forms
from django.http import QueryDict


class OrangeCheckForm(forms.Form):
    title = forms.CharField(max_length=200)
    description = forms.CharField(required=False)
    scheduled_at = forms.DateTimeField(required=False)  # read in the viewer's timezone
    max_points = forms.IntegerField(min_value=0, required=False)
    time_limit_minutes = forms.IntegerField(required=False, min_value=1)


class FollowUpForm(forms.Form):
    assignment_id = forms.IntegerField()
    minutes = forms.IntegerField(initial=15)
    note = forms.CharField(required=False)


class AssignmentRejectForm(forms.Form):
    notes = forms.CharField(required=False)


class ReassignForm(forms.Form):
    user_id = forms.IntegerField()


class SubmitScoreForm(forms.Form):
    score = forms.IntegerField(required=False, min_value=0)


class CriterionInput(TypedDict):
    id: int | None
    label: str
    points: int
    sort_order: int


def extract_criteria(post_data: QueryDict) -> list[CriterionInput]:
    """Parse criterion_label_{n} / criterion_points_{n} / criterion_id_{n} rows in n order.

    n can skip values: the form keeps a row's number when an earlier row is removed.
    Labels are cut to 200 characters (OrangeCheckCriterion.label); rows without a label
    or a positive integer point value are dropped.
    """
    numbers = sorted(int(m.group(1)) for key in post_data if (m := re.fullmatch(r"criterion_label_(\d+)", key)))
    criteria: list[CriterionInput] = []
    for n in numbers:
        label = post_data.get(f"criterion_label_{n}", "").strip()[:200]
        points_str = post_data.get(f"criterion_points_{n}", "").strip()
        id_str = post_data.get(f"criterion_id_{n}", "").strip()
        if not label or not points_str.isdigit() or int(points_str) < 1:
            continue
        criteria.append(
            {
                "id": int(id_str) if id_str.isdigit() else None,
                "label": label,
                "points": int(points_str),
                "sort_order": len(criteria),
            }
        )
    return criteria
