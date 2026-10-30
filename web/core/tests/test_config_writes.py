"""A CompetitionConfig is written only with the fields the write changes: the start, the stop and the timer write the
run state under the row lock, and a whole-row save of a copy read earlier would put their old values back."""

import ast

import pytest

from core.models import CompetitionConfig
from core.tests.guards import REPO_DIR, fail_with, tracked_files

# "path:function" -> why that save writes the whole row
EXCEPTIONS: dict[str, str] = {}


def _is_config_source(node: ast.expr | None) -> bool:
    return node is not None and "CompetitionConfig" in ast.unparse(node)


def whole_row_saves(source: str, path: str) -> dict[str, str]:
    """Each .save() or .asave() without update_fields on a name the function holds a CompetitionConfig in (read
    from an expression naming CompetitionConfig, or a parameter annotated with it), keyed by "path:function"."""
    found: dict[str, str] = {}
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        names = {a.arg for a in [*function.args.args, *function.args.kwonlyargs] if _is_config_source(a.annotation)}
        for node in ast.walk(function):
            if isinstance(node, ast.Assign | ast.AnnAssign) and _is_config_source(node.value):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("save", "asave")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in names
                and not any(k.arg == "update_fields" for k in node.keywords)
            ):
                found.setdefault(f"{path}:{function.name}", f"line {node.lineno}")
    return found


def test_config_saves_name_their_fields() -> None:
    found: dict[str, str] = {}
    for path in tracked_files():
        test_code = "/tests/" in path or "browser_tests/" in path or path.rsplit("/", 1)[-1].startswith("test_")
        if path.endswith(".py") and not test_code and "/migrations/" not in path:
            found |= whole_row_saves((REPO_DIR / path).read_text(encoding="utf-8"), path)
    fail_with("CompetitionConfig saved without update_fields", found, EXCEPTIONS)


def test_the_guard_finds_each_shape() -> None:
    source = """
def a():
    config = CompetitionConfig.get_config()
    config.save()
async def b():
    config = await sync_to_async(CompetitionConfig.get_config)()
    await config.asave()
def c(self, obj: CompetitionConfig):
    obj.save()
def d():
    (config, _) = CompetitionConfig.objects.get_or_create(pk=1)
    config.save()
def e():
    config = CompetitionConfig.locked()
    config.save()
def fine():
    config = CompetitionConfig.get_config()
    config.save(update_fields=["max_team_members", "updated_at"])
def other():
    team = Team.objects.get()
    team.save()
"""
    assert set(whole_row_saves(source, "m.py")) == {f"m.py:{name}" for name in "abcde"}


@pytest.mark.django_db
def test_the_admin_cannot_write_back_a_start_time_the_timer_cleared(admin_user) -> None:
    """The change form was loaded while a start was scheduled; the timer ran it and a manual stop followed. Posting
    the form must not put the start time back, or the timer would start the competition again."""
    from django.test import Client
    from django.urls import reverse

    CompetitionConfig.objects.update_or_create(
        pk=1, defaults={"competition_start_time": None, "applications_enabled": False, "last_run_incomplete": True}
    )
    client = Client()
    client.force_login(admin_user)

    response = client.post(
        reverse("admin:core_competitionconfig_change", args=[1]),
        {
            "competition_start_time_0": "2020-01-01",
            "competition_start_time_1": "09:00:00",
            "competition_end_time_0": "2099-01-01",
            "competition_end_time_1": "17:00:00",
            "controlled_applications": '["scoring"]',
            "max_team_members": "7",
        },
    )

    assert response.status_code == 302, response.content
    config = CompetitionConfig.objects.get(pk=1)
    assert (config.max_team_members, config.controlled_applications) == (10, [])
    assert (config.competition_start_time, config.competition_end_time) == (None, None)
    assert (config.applications_enabled, config.last_run_incomplete) == (False, True)
