from __future__ import annotations

from typing import TYPE_CHECKING

from slugify import slugify

if TYPE_CHECKING:
    from instagrapi import Client


_SLUG_MAX_LEN = 64


def _safe_slug(text: str) -> str:
    candidate = slugify(
        text or "",
        lowercase=True,
        max_length=_SLUG_MAX_LEN,
        separator="-",
    )
    return candidate or "_unknown"


def slugify_collection_name(name: str | None, fallback: str) -> str:
    """Transliterate a collection name into a safe slug.

    Used as the directory name under ``data/raw/`` instead of the numeric
    ``cid``. If ``name`` is empty or ``slugify`` collapses it to an empty
    string (e.g. the name was made up of special characters only),
    ``fallback`` is used — usually the ``cid`` itself or its string
    sentinel.

    Examples
    --------
    >>> slugify_collection_name("Furniture", "18427410172124759")
    'furniture'
    >>> slugify_collection_name("Textures: for home", "x")
    'textures-for-home'
    >>> slugify_collection_name("", "18427410172124759")
    '18427410172124759'
    >>> slugify_collection_name("ALL_MEDIA_AUTO_COLLECTION", "")
    'all-media-auto-collection'
    """
    if name:
        candidate = slugify(
            name,
            lowercase=True,
            max_length=_SLUG_MAX_LEN,
            separator="-",
        )
        if candidate:
            return candidate
    return _safe_slug(fallback)


def resolve_collection_name(client: "Client", collection_key: str | int) -> str:
    """Fetch the collection name from Instagram via ``iter_collections``.

    Used as a fallback in CLI scenarios where we do not have a
    ``Collection`` object at hand (e.g. ``insta-boards download`` takes
    ``--collection``): if the state file has no name, we walk every
    collection through the same cursor-pager the main sync engine uses.
    Any API failure just returns an empty string — the raw collection
    path will still work using ``collection_key`` as a fallback name.
    """
    target = str(collection_key)
    try:
        # Local import to avoid pulling the Instagram stack into light
        # unit tests.
        from .pagination import iter_collections

        for c in iter_collections(client):
            c_id = str(getattr(c, "id", "") or "")
            c_type = str(getattr(c, "type", "") or "")
            if c_id == target or (not target.isdigit() and c_type == target):
                return str(getattr(c, "name", "") or "")
    except Exception:
        return ""
    return ""
