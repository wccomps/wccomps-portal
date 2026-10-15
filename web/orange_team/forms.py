import re
from typing import TypedDict

from django import forms
from django.http import QueryDict

from orange_team.models import MAX_POINTS, OrangeCheckCriterion

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
# A rubric row's field as the check page names it: criterion_label_3, criterion_points_3, criterion_id_3
CRITERION_KEY = re.compile(r"criterion_(label|points|id)_([0-9]{1,9})")


class CriterionForm(forms.ModelForm[OrangeCheckCriterion]):
    """One rubric row of the check form; its limits are the model's."""

    class Meta:
        model = OrangeCheckCriterion
        fields = ["label", "points"]


class CriterionRow(TypedDict):
    """A rubric row as posted (and as the check form shows it again)."""

    id: str
    label: str
    points: str


class CriterionInput(TypedDict):
    id: int | None
    label: str
    points: int
    sort_order: int


def criterion_rows(post_data: QueryDict) -> list[CriterionRow]:
    """The posted criterion_label_{n} / criterion_points_{n} / criterion_id_{n} rows in n order.

    n can skip values: the form keeps a row's number when an earlier row is removed.
    """
    numbers = sorted({int(m.group(2)) for key in post_data if (m := CRITERION_KEY.fullmatch(key))})
    return [
        {
            "id": post_data.get(f"criterion_id_{n}", "").strip(),
            "label": post_data.get(f"criterion_label_{n}", ""),
            "points": post_data.get(f"criterion_points_{n}", "").strip(),
        }
        for n in numbers
    ]


def extract_criteria(post_data: QueryDict) -> list[CriterionInput]:
    """The posted rubric, each row checked by CriterionForm; an empty row is skipped.

    A row that doesn't pass refuses the whole rubric (ValidationError naming each such row): the edit replaces the
    check's criteria with the posted ones, so leaving a row out would delete it and its grading.
    """
    criteria: list[CriterionInput] = []
    problems: list[str] = []
    if any(key.startswith("criterion_") and not CRITERION_KEY.fullmatch(key) for key in post_data):
        problems.append("The page sent rubric fields it doesn't have; reload the page and enter the rubric again.")
    for position, row in enumerate(criterion_rows(post_data), start=1):
        if not (row["id"] or row["label"].strip() or row["points"]):
            continue
        form = CriterionForm({"label": row["label"], "points": row["points"]})
        name = f"Criterion {position} ({row['label'].strip()[:40] or 'no label'})"
        if row["id"] and not ROW_NUMBER.fullmatch(row["id"]):
            problems.append(f"{name}: the page sent an invalid row id; reload the page.")
        elif not form.is_valid():
            problems += [
                f"{name}: {field.capitalize()}: {error}" for field, errors in form.errors.items() for error in errors
            ]
        else:
            criteria.append(
                {
                    "id": int(row["id"]) if row["id"] else None,
                    "label": form.cleaned_data["label"],
                    "points": form.cleaned_data["points"],
                    "sort_order": len(criteria),
                }
            )
    if problems:
        raise forms.ValidationError(problems)
    return criteria
