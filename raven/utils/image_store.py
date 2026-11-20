"""The archived image refs a session replays.

A message's display content keeps ``[image]`` in place of an inline picture: the
bytes would otherwise bloat every transcript view and the session's text. But
the next turn has to send the same picture, character for character, or the
provider's cached prefix breaks at it and everything after it is prefilled
again.

The save pass therefore archives the exact data URI in a private
``_image_refs`` field beside the placeholder. The replay pass reads it back, so
what reaches the provider is the picture that was sent; ``content`` stays the
display form. The provider allow-list drops the refs, and the session view
never sees them.

Archiving trades session-load time for exact replay: the session loader parses
every line, so an image-heavy session opens more slowly than one holding only a
reference. The bytes stay in this private field, never in the display content,
so transcript views stay small.

A malformed reference leaves the placeholder: the turn still runs, and only the
cache entry behind the picture is lost.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from raven.utils.images import estimate_content_part_tokens, image_block, is_inline_image

IMAGE_REFS_KEY = "_image_refs"
"""References filed beside a saved message's image placeholders.

Each entry carries ``at`` (the block index), ``url`` (the exact data URI),
``tokens`` and the part's non-URL fields. Dropped by the provider allow-lists;
read only by the replay projection and the token estimator.
"""

IMAGE_PLACEHOLDER = "[image]"
"""What the display content holds in place of an inline picture."""


def capture_images(
    content: Any,
    *,
    drop: Callable[[Any], bool] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Replace inline pictures with placeholders, archiving their exact data URIs.

    A non-list comes back unchanged. ``drop`` removes blocks before the index a
    reference records, so a caller that also strips a runtime block gets indices
    into the list it actually stores.
    """
    if not isinstance(content, list):
        return content, []
    kept: list[Any] = []
    refs: list[dict[str, Any]] = []
    for part in content:
        if drop is not None and drop(part):
            continue
        if not is_inline_image(part):
            kept.append(part)
            continue
        ref = _ref_for_part(part)
        kept.append({"type": "text", "text": IMAGE_PLACEHOLDER})
        if ref is not None:
            ref["at"] = len(kept) - 1
            refs.append(ref)
    return kept, refs


def expand_images(content: Any, message: dict[str, Any]) -> Any:
    """``content`` with each recorded placeholder replaced by the picture it stood for.

    A message without references comes back as it is. A reference whose URL is
    malformed leaves the placeholder.
    """
    refs = message.get(IMAGE_REFS_KEY)
    if not isinstance(content, list) or not isinstance(refs, list):
        return content
    expanded = list(content)
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        at = ref.get("at")
        if not isinstance(at, int) or not 0 <= at < len(expanded):
            continue
        placeholder = expanded[at]
        if not (
            isinstance(placeholder, dict)
            and placeholder.get("type") == "text"
            and placeholder.get("text") == IMAGE_PLACEHOLDER
        ):
            continue
        rebuilt = _rebuild_part(ref)
        if rebuilt is not None:
            expanded[at] = rebuilt
    return expanded


def image_tokens(message: dict[str, Any]) -> int:
    """The image weight a saved message's references still cost the prompt."""
    refs = message.get(IMAGE_REFS_KEY)
    if not isinstance(refs, list):
        return 0
    total = 0
    for ref in refs:
        if isinstance(ref, dict):
            tokens = ref.get("tokens")
            if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0:
                total += tokens
    return total


def _ref_for_part(part: dict[str, Any]) -> dict[str, Any] | None:
    image_url = part.get("image_url")
    url = image_url.get("url") if isinstance(image_url, dict) else None
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return None
    ref: dict[str, Any] = {"url": url, "tokens": estimate_content_part_tokens(part) or 0}
    top = {key: value for key, value in part.items() if key not in ("type", "image_url")}
    url_extra = {key: value for key, value in image_url.items() if key != "url"}
    if top:
        ref["part"] = top
    if url_extra:
        ref["image_url"] = url_extra
    return ref


def _rebuild_part(ref: dict[str, Any]) -> dict[str, Any] | None:
    url = ref.get("url")
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return None
    rebuilt: dict[str, Any] = dict(image_block(url))
    extra = ref.get("part")
    if isinstance(extra, dict):
        rebuilt = {**rebuilt, **{key: value for key, value in extra.items() if key not in ("type", "image_url")}}
    extra_url = ref.get("image_url")
    if isinstance(extra_url, dict):
        rebuilt["image_url"] = {
            **rebuilt["image_url"],
            **{key: value for key, value in extra_url.items() if key != "url"},
        }
    return rebuilt


__all__ = [
    "IMAGE_PLACEHOLDER",
    "IMAGE_REFS_KEY",
    "capture_images",
    "expand_images",
    "image_tokens",
]
