"""Ticket categories read from the DB as TicketCategoryConfig dicts."""

from typing import TypedDict

from ticketing.models import TicketCategory


class TicketCategoryConfig(TypedDict, total=False):
    display_name: str
    points: int
    required_fields: list[str]
    optional_fields: list[str]
    variable_cost_note: str
    variable_points: bool
    min_points: int
    max_points: int
    user_creatable: bool
    playbook_url: str


def _model_to_config(cat: TicketCategory) -> TicketCategoryConfig:
    config: TicketCategoryConfig = {
        "display_name": cat.display_name,
        "points": cat.points,
        "required_fields": cat.required_fields,
        "optional_fields": cat.optional_fields,
        "variable_points": cat.variable_points,
        "user_creatable": cat.user_creatable,
    }
    if cat.variable_cost_note:
        config["variable_cost_note"] = cat.variable_cost_note
    if cat.min_points:
        config["min_points"] = cat.min_points
    if cat.max_points:
        config["max_points"] = cat.max_points
    if cat.playbook_url:
        config["playbook_url"] = cat.playbook_url
    return config


def get_category_config(category_id: int | None) -> TicketCategoryConfig | None:
    if category_id is None:
        return None
    try:
        cat = TicketCategory.objects.get(pk=category_id)
    except TicketCategory.DoesNotExist:
        return None
    return _model_to_config(cat)


def get_all_categories(
    user_creatable_only: bool = False,
) -> dict[int, TicketCategoryConfig]:
    qs = TicketCategory.objects.all()
    if user_creatable_only:
        qs = qs.filter(user_creatable=True)
    return {cat.pk: _model_to_config(cat) for cat in qs}
