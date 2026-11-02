"""Custom OAuth views for Authentik OIDC authentication."""

import base64
import json
import logging
import math
import secrets
import time
from datetime import datetime
from urllib.parse import urlencode

import httpx
from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.core import signing
from django.db import connection, models, transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from .auth_utils import get_role_based_landing_url, lock_account
from .models import CredentialsReset, UserGroups
from .utils import database_clock

logger = logging.getLogger(__name__)

STATE_EXPIRY_SECONDS = 300


def _get_oauth_config() -> dict[str, str]:
    client_id = getattr(settings, "AUTHENTIK_CLIENT_ID", None)
    client_secret = getattr(settings, "AUTHENTIK_SECRET", None)
    server_url = getattr(
        settings,
        "AUTHENTIK_OIDC_URL",
        "https://auth.wccomps.org/application/o/discord-bot/",
    )

    # Authentik uses shared endpoints (not per-application)
    # The application slug is only in the issuer and end-session URLs
    base_url = server_url.rstrip("/")
    auth_base = base_url.rsplit("/", 1)[0]  # Remove application slug
    return {
        "client_id": client_id or "",
        "client_secret": client_secret or "",
        "authorization_endpoint": f"{auth_base}/authorize/",
        "token_endpoint": f"{auth_base}/token/",
        "userinfo_endpoint": f"{auth_base}/userinfo/",
        "end_session_endpoint": f"{base_url}/end-session/",
    }


def oauth_login(request: HttpRequest) -> HttpResponse:
    config = _get_oauth_config()

    if not config["client_id"]:
        logger.error("AUTHENTIK_CLIENT_ID not configured")
        return render(
            request,
            "core/oauth_error.html",
            {"error_title": "Configuration Error", "error_message": "OAuth is not configured."},
            status=500,
        )

    next_url = request.GET.get("next", "/")
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = "/"

    # The signature keeps `next` tamper-proof and signing.loads(max_age=...) expires the state.
    # The nonce, also kept in this session, ties the callback to the browser that started the
    # login: without it anyone could send a victim through a callback carrying the attacker's
    # code and state, logging the victim in as the attacker (login CSRF).
    nonce = secrets.token_urlsafe(16)
    request.session["oauth_nonce"] = nonce
    state = signing.dumps({"n": nonce, "next": next_url, "t": int(time.time())})

    redirect_uri = request.build_absolute_uri("/auth/callback/")
    params = {
        "client_id": config["client_id"],
        "response_type": "code",
        "scope": "openid profile email groups",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    # After a refused sign-in from before a reset: make Authentik ask again rather than reuse its session
    if request.GET.get("prompt") == "login":
        params |= {"prompt": "login"}

    auth_url = f"{config['authorization_endpoint']}?{urlencode(params)}"
    return redirect(auth_url)


def _next_url(request: HttpRequest, state_data: dict[str, object]) -> str:
    """Where the sign-in began, from the signed state (checked again, in depth)."""
    next_url = state_data.get("next", "/")
    if not isinstance(next_url, str) or not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}
    ):
        return "/"
    return next_url


def _free_username(username: str, keep_pk: int | None = None) -> None:
    """Rename any other portal account holding this username. Call inside a transaction.

    Only the name changes: the account stays tied to its own Authentik sub, whose groups the
    periodic refresh keeps current (empty once that identity is deleted).
    """
    holders = User.objects.select_for_update().filter(username=username)
    if keep_pk is not None:
        holders = holders.exclude(pk=keep_pk)
    holder = holders.first()
    if not holder:
        return
    suffix = f"~replaced-{holder.pk}"
    max_length = User._meta.get_field("username").max_length or 150
    holder.username = username[: max_length - len(suffix)] + suffix
    holder.save(update_fields=["username"])
    logger.warning(
        f"Username '{username}' now belongs to another Authentik identity; renamed its holder to '{holder.username}'"
    )


def _json_object(response: httpx.Response) -> dict[str, object]:
    """The answer's JSON object; ValueError when it isn't one (not JSON, nested too deep to read, or another type)."""
    try:
        data = response.json()
    except RecursionError as e:
        raise ValueError("the answer is nested too deep to read") from e
    if not isinstance(data, dict):
        raise ValueError(f"the answer isn't a JSON object: {type(data).__name__}")
    return data


def _fits(text: str, model: type[models.Model], name: str) -> bool:
    length: int | None = getattr(model._meta.get_field(name), "max_length", None)
    return length is None or len(text) <= length


def _signed_in_at(tokens: dict[str, object], state_data: dict[str, object]) -> float:
    """When the person signed in to Authentik: the ID token's auth_time, else when this sign-in began here. The ID
    token comes straight from the token endpoint, so its claims are read without checking its signature."""
    id_token = tokens.get("id_token")
    try:
        payload = str(id_token).split(".")[1]
        auth_time = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("auth_time")
        if isinstance(auth_time, int | float) and not isinstance(auth_time, bool) and math.isfinite(auth_time):
            return float(auth_time)
    except IndexError, ValueError, AttributeError, RecursionError, OverflowError:
        pass
    started = state_data.get("t")
    return float(started) if isinstance(started, int | float) and not isinstance(started, bool) else 0.0


# auth_time is whole seconds and the two clocks differ a little; a sign-in this close before a reset counts as after
RESET_CLOCK_MARGIN = 2.0


def _before_reset(signed_in_at: float, reset_at: datetime) -> bool:
    return signed_in_at < reset_at.timestamp() - RESET_CLOCK_MARGIN


def _claims(userinfo: dict[str, object]) -> tuple[str, str, str, list[str]]:
    """The sub, username (the email when there is none), email and group names the account is made from;
    ValueError when they can't make one: missing, not text, a NUL, or longer than their columns."""
    sub = userinfo.get("sub")
    email = userinfo.get("email") or ""
    username = userinfo.get("preferred_username") or email
    groups = userinfo.get("groups", [])
    if not isinstance(sub, str) or not sub or not _fits(sub, UserGroups, "authentik_id"):
        raise ValueError("no usable sub claim")
    if not isinstance(username, str) or not username or not _fits(username, User, "username"):
        raise ValueError("no usable username")
    if not isinstance(email, str) or not _fits(email, User, "email"):
        raise ValueError("an unusable email")
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        raise ValueError("groups that aren't a list of names")
    if "\x00" in sub + username + email + "".join(groups):
        raise ValueError("a NUL character")
    return sub, username, email, groups


def oauth_callback(request: HttpRequest) -> HttpResponse:
    config = _get_oauth_config()

    error = request.GET.get("error")
    if error:
        error_description = request.GET.get("error_description", "Unknown error")
        if error == "access_denied":
            return render(
                request,
                "core/oauth_error.html",
                {"error_title": "Login Cancelled", "error_message": "You cancelled the login."},
            )
        logger.warning("OAuth error: %s - %s", error, error_description)
        return render(
            request,
            "core/oauth_error.html",
            {"error_title": "Login Failed", "error_message": "Please try again."},
        )

    # Verify the signed state, then that this browser's session started the login (see oauth_login).
    raw_state = request.GET.get("state")
    if not raw_state:
        logger.warning("OAuth state missing")
        return render(
            request,
            "core/oauth_error.html",
            {"error_title": "Session Expired", "error_message": "Please try again."},
        )

    try:
        state_data = signing.loads(raw_state, max_age=STATE_EXPIRY_SECONDS)
    except signing.SignatureExpired:
        logger.warning("OAuth state expired")
        return render(
            request,
            "core/oauth_error.html",
            {"error_title": "Session Expired", "error_message": "Please try again."},
        )
    except signing.BadSignature:
        logger.warning("OAuth state signature invalid")
        return render(
            request,
            "core/oauth_error.html",
            {"error_title": "Security Error", "error_message": "Please try again."},
        )

    session_nonce = request.session.pop("oauth_nonce", None)
    if not session_nonce or not secrets.compare_digest(str(state_data.get("n", "")), session_nonce):
        logger.warning("OAuth state was not issued to this session")
        return render(
            request,
            "core/oauth_error.html",
            {"error_title": "Session Expired", "error_message": "Please try logging in again."},
        )

    code = request.GET.get("code")
    if not code:
        logger.warning("OAuth code missing")
        return render(
            request,
            "core/oauth_error.html",
            {"error_title": "Login Failed", "error_message": "Please try again."},
        )

    redirect_uri = request.build_absolute_uri("/auth/callback/")
    try:
        with httpx.Client(timeout=settings.HTTPX_DEFAULT_TIMEOUT) as client:
            token_response = client.post(
                config["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "client_id": config["client_id"],
                    "client_secret": config["client_secret"],
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
            token_response.raise_for_status()
            tokens = _json_object(token_response)
    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"Token exchange failed: {e}")
        return render(
            request,
            "core/oauth_error.html",
            {"error_title": "Authentication Failed", "error_message": "Please try again."},
        )

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        logger.error("No access token in response")
        return render(
            request,
            "core/oauth_error.html",
            {"error_title": "Authentication Failed", "error_message": "Please try again."},
        )

    read_at = database_clock()
    try:
        with httpx.Client(timeout=settings.HTTPX_DEFAULT_TIMEOUT) as client:
            userinfo_response = client.get(
                config["userinfo_endpoint"],
                headers={"Authorization": f"Bearer {access_token}"},
            )
            userinfo_response.raise_for_status()
            userinfo = _json_object(userinfo_response)
    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"Userinfo fetch failed: {e}")
        return render(
            request,
            "core/oauth_error.html",
            {"error_title": "Authentication Failed", "error_message": "Please try again."},
        )

    try:
        authentik_id, username, email, groups = _claims(userinfo)
    except ValueError as e:
        logger.error(f"Userinfo unusable: {e}")
        return render(
            request,
            "core/oauth_error.html",
            {"error_title": "Authentication Failed", "error_message": "Invalid user data."},
        )

    next_url = _next_url(request, state_data)
    # Accounts are keyed by the Authentik sub only: usernames get reused, and matching on
    # them would hand a new identity someone else's account
    with transaction.atomic():
        # A team account's members often log in at the same moment: without this, two first
        # logins both see no account and the second create fails on the unique username.
        lock_account(authentik_id)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"username:{username}"])
        user_groups = UserGroups.objects.select_related("user").filter(authentik_id=authentik_id).first()
        # Read under the account's lock, which a reset's record takes too: a code issued before the reset, redeemed
        # after, would undo it
        reset = CredentialsReset.objects.filter(authentik_id=authentik_id).first()
        if reset and _before_reset(_signed_in_at(tokens, state_data), reset.reset_at):
            logger.warning("Sign-in from before a credentials reset refused")
            return render(
                request,
                "core/oauth_error.html",
                {
                    "error_title": "Signed Out",
                    "error_message": "This account's password was reset after you signed in. Sign in again with it.",
                    "retry_url": reverse("oauth_login") + "?" + urlencode({"prompt": "login", "next": next_url}),
                },
            )
        if user_groups is None:
            _free_username(username)
            user = User.objects.create(username=username, email=email)
            UserGroups.objects.create(user=user, authentik_id=authentik_id, groups=groups, groups_updated_at=read_at)
        else:
            user = user_groups.user
            if user.username != username:
                _free_username(username, keep_pk=user.pk)
                user.username = username
                user.save(update_fields=["username"])
            # A refresh that read Authentik after this sign-in did keeps what it read, changed or not
            UserGroups.objects.filter(pk=user_groups.pk).exclude(groups_updated_at__gt=read_at).update(
                groups=groups, account_active=True, groups_updated_at=read_at
            )

    login(request, user)

    if next_url == "/":
        next_url = get_role_based_landing_url(groups)

    return redirect(next_url)


def oauth_logout(request: HttpRequest) -> HttpResponse:
    """Log out user and redirect to Authentik end session."""
    config = _get_oauth_config()

    logout(request)

    redirect_uri = request.build_absolute_uri("/")
    params = {"post_logout_redirect_uri": redirect_uri}
    logout_url = f"{config['end_session_endpoint']}?{urlencode(params)}"

    return redirect(logout_url)
