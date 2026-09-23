"""Podly API client for whitelisting and triggering episode processing."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import TypedDict

import requests

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


class PodlyFeedSummary(TypedDict, total=False):
    """Summary representation of a Podly feed."""

    id: int
    title: str


class PodlyPostSummary(TypedDict, total=False):
    """Summary representation of a Podly post."""

    guid: str
    download_url: str
    title: str


class PodlyFeedPostsResponse(TypedDict, total=False):
    """Response payload for Podly feed posts query."""

    items: list[PodlyPostSummary]


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
    """Search Podly feeds and their posts for a matching episode GUID.

    Returns:
        The matched episode GUID, or None if not found.

    """
    try:
        feeds_res = session.get(f"{base_url}/feeds", timeout=timeout)
        if feeds_res.status_code != 200:
            return None
        feeds_data: list[PodlyFeedSummary] = json.loads(feeds_res.text)  # pyright: ignore[reportAny]
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Failed to fetch feeds from Podly at %s: %s", base_url, exc)
        return None

    target_feeds: list[PodlyFeedSummary] = []
    other_feeds: list[PodlyFeedSummary] = []
    for feed_item in feeds_data:
        f_title = feed_item.get("title") or ""
        if feed_name and feed_name.lower() in f_title.lower():
            target_feeds.append(feed_item)
        else:
            other_feeds.append(feed_item)

    candidate_feeds = target_feeds or (target_feeds + other_feeds)
    normalized_title = title.strip().lower() if title else ""

    for feed_info in candidate_feeds:
        feed_id = feed_info.get("id")
        if feed_id is None:
            continue
        try:
            posts_res = session.get(f"{base_url}/api/feeds/{feed_id}/posts?page_size=50", timeout=timeout)
            if posts_res.status_code != 200:
                continue
            posts_data: PodlyFeedPostsResponse = json.loads(posts_res.text)  # pyright: ignore[reportAny]
            posts_items = posts_data.get("items") or []
            for post_item in posts_items:
                p_guid = post_item.get("guid") or ""
                p_url = post_item.get("download_url") or ""
                p_title = (post_item.get("title") or "").strip().lower()

                if guid and p_guid == guid:
                    return p_guid
                if download_url and p_url == download_url:
                    return p_guid
                if normalized_title and (normalized_title in p_title or p_title in normalized_title):
                    return p_guid
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Failed to query posts for feed %s in Podly: %s", feed_id, exc)

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
                f"{base_url}/api/posts/{target_guid}/whitelist",
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
            f"{base_url}/api/posts/{discovered_guid}/whitelist",
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
