"""Prove that controlled Authentik apps exclude the accounts a packet may enable."""

from collections.abc import Collection, Iterable, Mapping

from core.authentik_manager import AuthentikAPIError, AuthentikManager, is_blueteam_binding
from core.services.user_groups import with_ancestors
from core.team_accounts import blueteam_kinds


def check_prestart_app_access(
    manager: AuthentikManager, slugs: Collection[str], users: Iterable[Mapping[str, object]], *, closing: bool = False
) -> None:
    """Raise when a controlled app can't be shown to deny these accounts with its BlueTeam binding off.

    In readiness, closing=True checks the expected bindings after Stop rather than requiring them already off.
    """
    if not slugs:
        raise AuthentikAPIError("No controlled applications configured")
    manager.require_global_binding_view()
    memberships = _team_memberships(manager, users)
    kinds = blueteam_kinds()
    for slug in slugs:
        app = manager.get_application_by_slug(slug)
        if app is None or app.get("slug") != slug:
            raise AuthentikAPIError(f"Controlled application '{slug}' was not found")
        app_fields: Mapping[str, object] = app
        if app_fields.get("policy_engine_mode") not in ("any", "all"):
            raise AuthentikAPIError(f"Controlled application '{slug}' has an unknown policy engine mode")
        bindings = manager.app_bindings(app["pk"], require_complete=True)
        blue = [binding for binding in bindings if any(is_blueteam_binding(binding, kind) for kind in kinds)]
        if not blue:
            raise AuthentikAPIError(f"Controlled application '{slug}' has no BlueTeam binding")
        if len(blue) > 1:
            raise AuthentikAPIError(
                f"Controlled application '{slug}' has more than one BlueTeam binding; remove extras"
            )
        blue_binding = blue[0]
        enabled = blue_binding.get("enabled")
        if type(enabled) is not bool or (enabled and not closing):
            raise AuthentikAPIError(f"Controlled application '{slug}' does not have its BlueTeam binding disabled")

        # With no enforcing bindings Authentik may admit everyone. Restrict the proof to static group
        # checks: expressions or negated/temporary bindings aren't a durable denial for an account about to be enabled.
        allowed_groups: set[str] = set()
        for binding in bindings:
            if binding is blue_binding or binding.get("enabled") is False:
                continue
            fields: Mapping[str, object] = binding
            group = fields.get("group")
            if not (
                binding.get("enabled") is True
                and isinstance(group, str)
                and group
                and "policy" in fields
                and fields["policy"] is None
                and "user" in fields
                and fields["user"] is None
                and all(fields.get(flag) is False for flag in ("negate", "dry_run", "expiring"))
            ):
                raise AuthentikAPIError(
                    f"Controlled application '{slug}' needs enabled non-BlueTeam bindings to non-negated, "
                    "non-expiring group memberships that are enforced (not dry run)"
                )
            allowed_groups.add(group)
        if not allowed_groups:
            raise AuthentikAPIError(
                f"Controlled application '{slug}' needs an enabled binding besides BlueTeam that excludes team accounts"
            )
        if memberships & allowed_groups:
            raise AuthentikAPIError(f"A team account belongs to an enabled access group for '{slug}'")


def _team_memberships(manager: AuthentikManager, users: Iterable[Mapping[str, object]]) -> set[str]:
    direct: set[str] = set()
    for user in users:
        groups = user.get("groups")
        if (
            user.get("type") not in ("internal", "external")
            or not isinstance(groups, list)
            or not all(isinstance(group, str) and group for group in groups)
        ):
            raise AuthentikAPIError("Can't verify the team account's type or Authentik group membership")
        direct.update(groups)
    if not direct:
        return direct
    parents = {group["pk"]: group["parents"] for group in manager.list_all_groups()}
    held = with_ancestors(direct, parents)
    if not direct <= held or any(parent not in held for group in held for parent in parents[group]):
        raise AuthentikAPIError("Can't verify the team account's inherited Authentik group membership")
    return held
