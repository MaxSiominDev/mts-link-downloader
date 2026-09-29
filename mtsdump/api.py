import json
from collections.abc import Callable

from mtsdump import net
from mtsdump.links import RecordRef

AUTH_STATUSES = frozenset({401, 403})
COOKIE_HOSTS = ("mts-link.ru", "webinar.ru")

CookieSource = Callable[[str], str | None]


class AccessDenied(Exception):
    pass


class ApiError(Exception):
    pass


def fetch_record(ref: RecordRef, cookie_for: CookieSource) -> dict:
    """Reads record metadata, falling back to the cookie-authenticated gateway for a private record."""
    path = f"/api/eventsessions/{ref.session_id}/record"
    try:
        return _get_json(f"https://{ref.host}{path}")
    except net.HttpStatusError as error:
        if error.status == 404:
            raise ApiError(f"record {ref.session_id} not found") from error
        if error.status not in AUTH_STATUSES:
            raise
    gateway = _gateway_host(ref.host)
    # The host comes from the link, so a lookalike link must not get the session cookies.
    if not any(gateway == domain or gateway.endswith(f".{domain}") for domain in COOKIE_HOSTS):
        raise AccessDenied(f"cookies go only to {' and '.join(COOKIE_HOSTS)} hosts, not to {gateway}")
    cookie = cookie_for(gateway)
    if cookie is None:
        raise AccessDenied("the record is open to participants only, pass --cookies-from-browser chrome or --cookie")
    try:
        return _get_json(f"https://{gateway}{path}", {"Cookie": cookie})
    except net.HttpStatusError as error:
        if error.status in AUTH_STATUSES:
            raise AccessDenied(
                f"{gateway} rejected the cookies, log in to MTS Link in the browser again or copy a fresh cookie"
            ) from error
        raise


def _gateway_host(host: str) -> str:
    # my.mts-link.ru answers 403 to private records even with valid cookies, only the gw host checks them.
    return "gw." + ".".join(host.split(".")[-2:])


def _get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    body = net.get(url, headers=headers)
    try:
        return json.loads(body)
    except ValueError as error:
        raise ApiError(f"{url} did not return JSON") from error
