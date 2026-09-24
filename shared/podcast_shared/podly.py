"""Podly API client for whitelisting and triggering episode processing."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse

import requests

from podcast_shared.json_narrow import is_json_array, is_json_object

logger = logging.getLogger(__name__)

DEFAULT_PODLY_URL = "http://localhost:5001"


def get_podly_config(
    url: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> tuple[str, str | None, str | None]:
    """Resolve Podly URL and credentials from parameters or environment.

    Returns:
        Tuple of (resolved_url, resolved_username, resolved_password).

    """
    resolved_url = (url or os.environ.get("PODLY_URL") or DEFAULT_PODLY_URL).rstrip("/")
    resolved_username = username or os.environ.get("PODLY_USERNAME")
    resolved_password = password or os.environ.get("PODLY_PASSWORD")
    return resolved_url, resolved_username, resolved_password


def _get_json(session: requests.Session, url: str, timeout: float) -> object | None:
    """GET ``url`` and decode its JSON body.

    Returns:
        The decoded JSON value, or None on a non-200 response or request/decode failure.

    """
    try:
        res = session.get(url, timeout=timeout)
        if res.status_code != 200:
            logger.warning("Podly GET %s returned HTTP %s", url, res.status_code)
            return None
        decoded: object = json.loads(res.text)  # pyright: ignore[reportAny]  (JSON boundary; narrowed by callers)
        return decoded
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Podly GET %s failed: %s", url, exc)
        return None


def _str_field(item: dict[str, object], key: str) -> str:
    """Return ``item[key]`` when it is a string, else ''.

    Returns:
        The string value or ''.

    """
    value = item.get(key)
    return value if isinstance(value, str) else ""


def _norm_title(title: str) -> str:
    """Casefold and collapse whitespace so titles compare exactly but leniently.

    Returns:
        The normalized title.

    """
    return " ".join(title.split()).casefold()


def _feed_posts(session: requests.Session, base_url: str, feed_id: int, timeout: float) -> list[dict[str, object]]:
    """Fetch one feed's posts (first page).

    Returns:
        The post dicts; empty when the request fails or the payload is malformed.

    """
    data = _get_json(session, f"{base_url}/api/feeds/{feed_id}/posts?page_size=50", timeout)
    if not is_json_object(data):
        return []
    items = data.get("items")
    if not is_json_array(items):
        return []
    return [item for item in items if is_json_object(item)]


def _whitelist_url(base_url: str, guid: str) -> str:
    """Build the whitelist endpoint URL, percent-encoding the GUID.

    RSS GUIDs are often URLs; unencoded, a ``?`` or ``#`` would truncate the path. (A GUID
    containing ``/`` still can't reach Podly's ``<string:>`` route, which is a server-side limit.)

    Returns:
        The endpoint URL.

    """
    return f"{base_url}/api/posts/{urllib.parse.quote(guid, safe='')}/whitelist"


def _find_post_guid_in_feeds(
    session: requests.Session,
    base_url: str,
    *,
    guid: str | None = None,
    download_url: str | None = None,
    title: str | None = None,
    feed_name: str | None = None,
    timeout: float = 15.0,
) -> str | None:
    """Search Podly's posts for the episode, strongest identifier first.

    Candidates come from the feeds whose title contains ``feed_name``; if none match, every
    feed is searched but only by GUID or download URL. An exact (normalized) title match is
    trusted only inside a name-matched feed, since episode titles repeat across shows.
    Match order across *all* candidate posts: exact GUID, exact download URL, exact title.

    Returns:
        The matched episode GUID, or None if not found.

    """
    feeds = _get_json(session, f"{base_url}/feeds", timeout)
    if not is_json_array(feeds):
        logger.warning("Podly /feeds returned an unexpected payload; cannot search for episode")
        return None
    feed_ids: list[tuple[int, str]] = []
    for feed in feeds:
        if is_json_object(feed):
            feed_id = feed.get("id")
            if isinstance(feed_id, int):
                feed_ids.append((feed_id, _str_field(feed, "title")))

    wanted = feed_name.casefold() if feed_name else ""
    named = [fid for fid, ftitle in feed_ids if wanted and wanted in ftitle.casefold()]
    posts = [
        post for fid in (named or [fid for fid, _ in feed_ids]) for post in _feed_posts(session, base_url, fid, timeout)
    ]

    matchers: list[tuple[str, str]] = [("guid", guid or ""), ("download_url", download_url or "")]
    if named:
        matchers.append(("title", _norm_title(title or "")))
    for key, target in matchers:
        if not target:
            continue
        for post in posts:
            value = _str_field(post, key)
            if (_norm_title(value) if key == "title" else value) == target:
                matched = _str_field(post, "guid")
                if matched:
                    logger.info("Matched Podly post by %s: %s", key, matched)
                    return matched
    return None


def enable_post_in_podly(
    *,
    guid: str | None = None,
    download_url: str | None = None,
    title: str | None = None,
    feed_name: str | None = None,
    podly_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 15.0,
) -> bool:
    """Enable an episode for processing in Podly directly.

    Logs the action and outcome. Sends no user notifications.

    Returns:
        True if the episode was found and enabled/started in Podly, False otherwise.

    """
    base_url, user, pwd = get_podly_config(podly_url, username, password)
    display_name = title or guid or download_url or "unknown episode"
    logger.info("Enabling episode for processing in Podly directly: %s (GUID: %s)", display_name, guid or "unknown")

    session = requests.Session()

    if user and pwd:
        try:
            login_res = session.post(
                f"{base_url}/api/auth/login",
                json={"username": user, "password": pwd},
                timeout=timeout,
            )
            if login_res.status_code != 200:
                logger.error(
                    "Failed to authenticate with Podly at %s: HTTP %s (%s)",
                    base_url,
                    login_res.status_code,
                    login_res.text[:200],
                )
                return False
        except requests.RequestException:
            logger.exception("Error connecting to Podly auth at %s", base_url)
            return False

    target_guid = guid

    # Try direct whitelist if GUID is already known
    if target_guid:
        try:
            whitelist_res = session.post(
                _whitelist_url(base_url, target_guid),
                json={"whitelisted": True, "trigger_processing": True},
                timeout=timeout,
            )
            if whitelist_res.status_code == 200:
                logger.info(
                    "Successfully enabled episode in Podly: %s (GUID: %s)",
                    display_name,
                    target_guid,
                )
                return True
            if whitelist_res.status_code != 404:
                logger.warning(
                    "Podly whitelist returned HTTP %s for GUID %s: %s",
                    whitelist_res.status_code,
                    target_guid,
                    whitelist_res.text[:200],
                )
        except requests.RequestException:
            logger.exception("Error calling Podly whitelist for GUID %s", target_guid)
            return False

    # If post not found or GUID not provided, trigger feed refresh in Podly and search
    logger.info("Refreshing feeds in Podly to discover episode: %s", display_name)
    try:
        _ = session.post(f"{base_url}/api/feeds/refresh-all", timeout=timeout)
        time.sleep(2.0)
    except requests.RequestException as exc:
        logger.warning("Podly feed refresh request failed: %s", exc)

    discovered_guid = _find_post_guid_in_feeds(
        session,
        base_url,
        guid=guid,
        download_url=download_url,
        title=title,
        feed_name=feed_name,
        timeout=timeout,
    )

    if not discovered_guid:
        logger.warning(
            "Could not find episode in Podly to enable: title=%r, guid=%r, download_url=%r",
            title,
            guid,
            download_url,
        )
        return False

    try:
        whitelist_res = session.post(
            _whitelist_url(base_url, discovered_guid),
            json={"whitelisted": True, "trigger_processing": True},
            timeout=timeout,
        )
        if whitelist_res.status_code == 200:
            logger.info(
                "Successfully enabled episode in Podly: %s (GUID: %s)",
                display_name,
                discovered_guid,
            )
            return True
        logger.warning(
            "Podly whitelist returned HTTP %s for discovered GUID %s: %s",
            whitelist_res.status_code,
            discovered_guid,
            whitelist_res.text[:200],
        )
        return False
    except requests.RequestException:
        logger.exception("Error calling Podly whitelist for discovered GUID %s", discovered_guid)
        return False
