# SPDX-License-Identifier: AGPL-3.0-or-later
"""Convert Discord Tenor GIF embeds into Fluxer/KLIPY GIF embeds.

Tenor is shutting down. Fluxer's active GIF provider is KLIPY, and a natively
sent KLIPY GIF is stored as a message whose content is a ``klipy.com/gifs/<slug>``
link plus a single ``gifv`` embed whose media points at **KLIPY's own CDN**
(``static.klipy.com``) - nothing is uploaded to the instance's S3.

Tenor and KLIPY are separate libraries with separate id/slug namespaces (KLIPY's
"1:1 replacement" refers to the *API shape*, not shared content - proven: Tenor
ids return an empty ``posts?ids=`` list and Tenor slugs 404 on KLIPY). So a
historical ``tenor.com/view/...`` link has no deterministic KLIPY equivalent; the
only bridge is search. We use the text already baked into the Tenor slug (which
we still have in ``backup.db``, so this works after Tenor is gone) as the query,
re-rank the results by positional-weighted token overlap (Tenor slugs lead with
the subject), and take the best match. The chosen GIF's media/dimensions come
straight from the search response, and the blur placeholder from KLIPY's own
``blur_preview`` - so building the embed needs **no media download and stores
nothing on the server**.

Results are cached on disk keyed by the query string, so a GIF reposted N times
resolves with one API call, and re-runs are free.
"""

import base64
import hashlib
import hmac
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request

KLIPY_API_BASE = "https://api.klipy.com/api/v1"
KLIPY_SHARE_ORIGIN = "https://klipy.com"

# KLIPY renditions, in the same preference order Fluxer's own resolver uses.
_SIZE_PREFERENCE = ("hd", "md", "sm", "xs")
_VIDEO_FORMATS = ("webm", "mp4")
_THUMBNAIL_FORMATS = ("webp", "gif")
_VIDEO_CONTENT_TYPE = {"webm": "video/webm", "mp4": "video/mp4"}
_THUMBNAIL_CONTENT_TYPE = {"webp": "image/webp", "gif": "image/gif"}
_ANIMATED_FLAG = 1 << 5  # matches Fluxer embed_media_flags (animated media)

# Query-side stopwords: filler that would otherwise let a long unrelated title
# accumulate spurious overlap. Kept deliberately tiny.
_STOPWORDS = {"a", "the", "gif", "of", "to", "and", "in", "on", "is", "it"}

_USER_AGENT = "flushcord-klipy/1.0"
_TENOR_VIEW_MARKER = "tenor.com/view/"
# A bare tenor link inside message content, for replacement/stripping.
_TENOR_LINK_RE = re.compile(r"https?://(?:www\.)?tenor\.com/view/\S+", re.I)


def is_tenor_view_url(url):
    """True for a ``tenor.com/view/...`` GIF page URL."""
    return bool(url) and _TENOR_VIEW_MARKER in url.lower()


def tenor_query(url):
    """Extract ``(query_words, tenor_id)`` from a Tenor view URL.

    ``tenor.com/view/unlimited-power-star-wars-gif-10270127`` ->
    ``(["unlimited", "power", "star", "wars"], "10270127")``. Returns
    ``(None, None)`` when the URL isn't a parseable Tenor view link.
    """
    m = re.search(r"tenor\.com/view/(.+)$", url or "", re.I)
    if not m:
        return None, None
    tail = m.group(1).strip().strip("/")
    tid = None
    idm = re.search(r"-gif-(\d+)$", tail)
    if idm:
        tid = idm.group(1)
        tail = tail[: idm.start()]
    # Slugs with non-Latin text are percent-encoded (e.g. Cyrillic/Japanese);
    # decode so the real words become the search query, not %XX hex noise.
    tail = urllib.parse.unquote(tail)
    words = [w for w in tail.split("-") if w and not w.isdigit()]
    return (words or None), tid


def share_url(slug):
    """The canonical KLIPY share URL Fluxer stores as the message content."""
    return f"{KLIPY_SHARE_ORIGIN}/gifs/{slug}"


_SHARE_SLUG_RE = re.compile(r"klipy\.com/(?:gifs|clips)/([^/?#\s]+)", re.I)


def slug_from_share_url(url):
    """Extract the ``<slug>`` from a ``klipy.com/gifs/<slug>`` share URL."""
    m = _SHARE_SLUG_RE.search(url or "")
    return urllib.parse.unquote(m.group(1)) if m else None


def _b64url_nopad(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_media_proxy_url(endpoint, secret, cdn_url):
    """Reproduce Fluxer's signed external media-proxy URL for ``cdn_url``.

    Mirrors ``fluxer_gifs``'s ``MediaProxyUrlBuilder::external_proxy_url``:
    ``{endpoint}/external/{b64url(HMAC_SHA256(path))}/{path}`` where
    ``path = "v2/" + b64url(cdn_url)``. If ``cdn_url`` is already on the proxy
    host it is returned unchanged. Returns None when endpoint/secret are unset.
    """
    if not endpoint or not secret or not cdn_url:
        return None
    endpoint = endpoint.rstrip("/")
    proxy_host = urllib.parse.urlparse(endpoint).hostname
    if proxy_host and urllib.parse.urlparse(cdn_url).hostname == proxy_host:
        return cdn_url
    path = "v2/" + _b64url_nopad(cdn_url.encode("utf-8"))
    sig = _b64url_nopad(
        hmac.new(secret.encode("utf-8"), path.encode("utf-8"), hashlib.sha256).digest()
    )
    return f"{endpoint}/external/{sig}/{path}"


def replace_tenor_link(content, tenor_url, klipy_url):
    """Swap the Tenor link in message content for the KLIPY share URL.

    Falls back to a regex swap of any tenor view link if the exact URL text
    isn't present verbatim (Discord occasionally normalises it).
    """
    if not content:
        return klipy_url
    if tenor_url and tenor_url in content:
        return content.replace(tenor_url, klipy_url)
    if _TENOR_LINK_RE.search(content):
        return _TENOR_LINK_RE.sub(klipy_url, content, count=1)
    return content


def _tokens(text):
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if w and w not in _STOPWORDS}


def _placeholder_from_blur(blur_preview):
    """Compute a ThumbHash placeholder from KLIPY's base64 ``blur_preview`` JPEG.

    Returns None if the field is absent or Pillow/thumbhash can't decode it;
    the placeholder is optional, so a miss just means no blur-up preview.
    """
    if not blur_preview or "," not in blur_preview:
        return None
    try:
        from PIL import Image

        from thumbhash import image_placeholder

        raw = base64.b64decode(blur_preview.split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as img:
            return image_placeholder(img.convert("RGBA"))
    except Exception:  # noqa: BLE001
        return None


def _pick_rendition(gif, formats):
    """Best ``(format, media_obj)`` for a GIF across size x format preference."""
    files = gif.get("file") or {}
    for size in _SIZE_PREFERENCE:
        block = files.get(size) or {}
        for fmt in formats:
            media = block.get(fmt)
            if isinstance(media, dict) and media.get("url"):
                return fmt, media
    return None


class KlipyClient:
    """Resolves Tenor links to KLIPY ``gifv`` embeds, with an on-disk cache."""

    def __init__(self, api_key, customer_id="flushcord", cache_path=None,
                 per_page=10, verbose=False, proxy_endpoint=None, proxy_secret=None):
        self.api_key = (api_key or "").strip()
        self.customer_id = customer_id or "flushcord"
        self.per_page = per_page
        self.cache_path = cache_path
        self.verbose = verbose
        # Media-proxy signing config; when set, built embeds carry a signed
        # proxy_url (which Fluxer's client requires to render the media).
        self.proxy_endpoint = (proxy_endpoint or "").strip() or None
        self.proxy_secret = (proxy_secret or "").strip() or None
        self.api_calls = 0
        # query string -> embed dict, or None for a cached miss.
        self._cache = {}
        if cache_path and os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    self._cache = json.load(f)
            except (ValueError, OSError):
                self._cache = {}

    # -- cache -------------------------------------------------------------- #

    def save_cache(self):
        if not self.cache_path:
            return
        tmp = self.cache_path + ".part"
        with open(tmp, "w") as f:
            json.dump(self._cache, f, ensure_ascii=False)
        os.replace(tmp, self.cache_path)

    # -- resolution --------------------------------------------------------- #

    def resolve_embed(self, tenor_url):
        """Return a KLIPY ``gifv`` embed dict for a Tenor URL, or None on miss.

        A cached None means "searched, no usable match" and is not retried.
        """
        words, _ = tenor_query(tenor_url)
        if not words:
            return None
        key = " ".join(words)
        if key in self._cache:
            return self._cache[key]
        gif = self._pick(words, self._search(key))
        embed = self._build_embed(gif) if gif else None
        self._cache[key] = embed
        # Cache may predate proxy signing; ensure proxy_url on every return.
        return self._ensure_proxy_urls(embed)

    def resolve_by_slug(self, slug, kind="gifs"):
        """Resolve a KLIPY share slug directly to a ``gifv`` embed, or None.

        Used to upgrade degraded ``rich`` embeds (a bare ``klipy.com`` link that
        Fluxer unfurled without dimensions) into a proper native ``gifv``.
        """
        gif = self._resolve_slug(slug, kind)
        return self._build_embed(gif) if gif else None

    def _ensure_proxy_urls(self, embed):
        """Populate a signed proxy_url on the embed's media (idempotent)."""
        if not embed or not (self.proxy_endpoint and self.proxy_secret):
            return embed
        for media in (embed.get("video"), embed.get("thumbnail")):
            if isinstance(media, dict) and media.get("url") and not media.get("proxy_url"):
                media["proxy_url"] = sign_media_proxy_url(
                    self.proxy_endpoint, self.proxy_secret, media["url"]
                )
        return embed

    def _resolve_slug(self, slug, kind="gifs", retries=3):
        if not slug:
            return None
        url = (f"{KLIPY_API_BASE}/{self.api_key}/{kind}/"
               f"{urllib.parse.quote(slug)}?customer_id="
               f"{urllib.parse.quote(self.customer_id)}")
        for attempt in range(retries):
            try:
                self.api_calls += 1
                req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
                with urllib.request.urlopen(req, timeout=25) as r:
                    data = json.load(r)
                if not data.get("result"):
                    return None
                item = data.get("data") or {}
                # Some responses nest the item one level deeper.
                if isinstance(item, dict) and not item.get("slug") and isinstance(item.get("data"), dict):
                    item = item["data"]
                return item if isinstance(item, dict) and item.get("file") else None
            except Exception:  # noqa: BLE001
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
        return None

    def _search(self, query, retries=3):
        params = urllib.parse.urlencode({
            "q": query,
            "per_page": self.per_page,
            "customer_id": self.customer_id,
        })
        url = f"{KLIPY_API_BASE}/{self.api_key}/gifs/search?{params}"
        for attempt in range(retries):
            try:
                self.api_calls += 1
                req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
                with urllib.request.urlopen(req, timeout=25) as r:
                    data = json.load(r)
                if data.get("result"):
                    return (data.get("data") or {}).get("data") or []
                return []
            except Exception:  # noqa: BLE001
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
        return []

    @staticmethod
    def _score(words, gif):
        """Positional-weighted token overlap between the query and a candidate.

        Earlier query words weigh more (Tenor slugs lead with the subject), so a
        generic trailing word can't drag the pick onto an unrelated GIF.
        """
        cand = _tokens(gif.get("slug", "")) | _tokens(gif.get("title", ""))
        seen = set()
        score = 0.0
        for i, w in enumerate(words):
            if w in _STOPWORDS or w in seen:
                continue
            seen.add(w)
            if w in cand:
                score += 1.0 / (1 + i)
        return score

    def _pick(self, words, results):
        """Highest-scoring candidate; None if nothing shares a token (no-match)."""
        best = None
        best_score = 0.0
        for gif in results:
            score = self._score(words, gif)
            if score > best_score:
                best_score = score
                best = gif
        return best

    def _build_embed(self, gif):
        video = _pick_rendition(gif, _VIDEO_FORMATS)
        thumbnail = _pick_rendition(gif, _THUMBNAIL_FORMATS)
        if not video and not thumbnail:
            return None
        slug = gif.get("slug") or ""
        placeholder = _placeholder_from_blur(gif.get("blur_preview"))
        embed = {
            "type": "gifv",
            "url": share_url(slug),
            "provider": {"name": "KLIPY", "url": KLIPY_SHARE_ORIGIN},
        }
        if video:
            fmt, media = video
            embed["video"] = {
                "url": media["url"],
                "width": media.get("width"),
                "height": media.get("height"),
                "content_type": _VIDEO_CONTENT_TYPE[fmt],
                "placeholder": placeholder,
                "flags": 0,
            }
        if thumbnail:
            fmt, media = thumbnail
            embed["thumbnail"] = {
                "url": media["url"],
                "width": media.get("width"),
                "height": media.get("height"),
                "content_type": _THUMBNAIL_CONTENT_TYPE[fmt],
                "placeholder": placeholder,
                "flags": _ANIMATED_FLAG,
            }
        return self._ensure_proxy_urls(embed)
