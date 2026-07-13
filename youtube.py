# SPDX-License-Identifier: AGPL-3.0-or-later
"""YouTube links -> Fluxer ``video`` embeds.

Discord's backup stores a YouTube link as a bare ``rich`` embed: a title, a
description, an author, and a thumbnail *url* with no dimensions. Fluxer's
client drops media it can't size, so migrating that as-is yields a title card
with no image and no player. Fluxer itself builds a much richer embed, from the
YouTube Data API, in ``fluxer_unfurl/src/resolvers/youtube.rs``. This module
reproduces that resolver so a migrated link is indistinguishable from one posted
natively.

Parity notes -- all mirrored from Fluxer, none of it guessed:

* Shape and limits come from ``youtube.rs`` + ``embed_normalizer.rs`` +
  ``text_limits.rs``: colour 0xFF0000; description cut to 350 chars with a
  literal ``...`` then clamped to 20 lines; title and author name cut to 256
  with ``…``; thumbnail picked maxres > high > standard > medium > default; the
  player size parsed out of the API's ``player.embedHtml`` (1280x720 if absent).
* The thumbnail's ``content_hash`` / ``placeholder`` / dimensions come from the
  *same* media-proxy ``/_metadata`` call the resolver makes, so they are what
  Fluxer would have stored rather than something re-derived here.
* No ``proxy_url``. Fluxer never persists one: ``EmbedService.mapResponseMedia``
  drops it before the row is written, and ``fluxer_messages`` re-signs it from
  ``media.url`` on every read (``map_embed_media`` -> ``external_media_proxy_url``).
  So embeds here carry exactly the keys a Fluxer-written row carries -- verified
  against a real YouTube message posted into the target instance.
"""

import copy
import json
import os
import re
import urllib.parse
import urllib.request

# fluxer_unfurl/src/resolvers/youtube.rs
YOUTUBE_COLOR = 0xFF0000
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_DESCRIPTION_MAX = 350
YOUTUBE_HOSTS = (
    "www.youtube.com",
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "www.youtube-nocookie.com",
    "youtube-nocookie.com",
    "youtu.be",
)
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,15}$")

# fluxer_unfurl/src/text_limits.rs
TITLE_MAX = 256
AUTHOR_NAME_MAX = 256
DESCRIPTION_MAX = 4096
DESCRIPTION_LINES_MAX = 20

# Fluxer writes the provider url with a trailing slash (the resolver's bare
# "https://www.youtube.com" is normalised by a URL round-trip on the way in).
# Taken from a real Fluxer-written row rather than from the resolver source.
PROVIDER_URL = "https://www.youtube.com/"


# --------------------------------------------------------------------------- #
# URL parsing (youtube.rs: parse_youtube_url / extract_video_id / timestamp)
# --------------------------------------------------------------------------- #

def parse_youtube_url(url):
    """Return ``(video_id, start_seconds|None)`` for a YouTube URL, else None."""
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return None
    video_id = _extract_video_id(host, u)
    if not video_id or not _VIDEO_ID_RE.match(video_id):
        return None
    return video_id, _extract_timestamp(u)


def is_youtube_url(url):
    return parse_youtube_url(url or "") is not None


def _extract_video_id(host, u):
    path = u.path
    if host == "youtu.be":
        return path.lstrip("/").split("/")[0] or None
    for prefix in ("/shorts/", "/v/", "/embed/"):
        if path.startswith(prefix):
            vid = path[len(prefix):].split("/")[0]
            # Playlist/live players carry no single video.
            if prefix == "/embed/" and vid in ("videoseries", "live_stream"):
                return None
            return vid or None
    if path == "/watch" or path.startswith("/watch/"):
        return _query_first(u, "v")
    return None


def _extract_timestamp(u):
    raw = _query_first(u, "t") or _query_first(u, "start")
    if not raw:
        return None
    if raw.endswith("s"):
        raw = raw[:-1]
    try:
        return int(raw)
    except ValueError:
        return None


def _query_first(u, key):
    for k, v in urllib.parse.parse_qsl(u.query):
        if k == key:
            return v
    return None


# --------------------------------------------------------------------------- #
# Text limits (text_limits.rs)
# --------------------------------------------------------------------------- #

def _truncate(s, max_len):
    """text_limits::truncate -- count characters, elide with a single U+2026."""
    if len(s) <= max_len:
        return s
    return s[:max(max_len - 1, 0)] + "…"


def _clamp_lines(s, max_lines):
    """text_limits::clamp_lines -- keep the first ``max_lines`` lines, then U+2026."""
    lines = _rust_lines(s)
    if len(lines) <= max_lines:
        return s  # unchanged, so \r\n line endings survive as Fluxer leaves them
    return "\n".join(lines[:max_lines] + ["…"])


def _rust_lines(s):
    """Split like Rust's ``str::lines``: on \\n, stripping one trailing \\r."""
    lines = s.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _youtube_string(value, max_len=YOUTUBE_DESCRIPTION_MAX):
    """youtube.rs::parse_youtube_string -- trim, elide with a literal ``...``."""
    trimmed = (value or "").strip()
    if len(trimmed) <= max_len:
        return trimmed
    return trimmed[:max(max_len - 3, 0)] + "..."


def _embed_dimensions(embed_html):
    """youtube.rs::parse_player_embed_dimensions -- read the iframe's size."""
    def dim(name):
        m = re.search(rf'{name}="(\d+)"', embed_html or "")
        return int(m.group(1)) if m else None
    width, height = dim("width"), dim("height")
    if width is None or height is None:
        return 1280, 720
    return width, height


# --------------------------------------------------------------------------- #
# Media proxy metadata (fluxer_unfurl/src/media_proxy.rs: get_metadata)
# --------------------------------------------------------------------------- #

class MediaProxyClient:
    """Fluxer's media proxy ``/_metadata``: dimensions, hash, ThumbHash, flags.

    Same request the unfurler makes, so the values we persist are the ones
    Fluxer itself would have persisted (rather than a local re-derivation that
    could drift from the proxy's).
    """

    def __init__(self, endpoint, secret, nsfw_mode="block", timeout=20):
        self.endpoint = (endpoint or "").rstrip("/")
        self.secret = secret or ""
        self.nsfw_mode = nsfw_mode
        self.timeout = timeout

    def metadata(self, url):
        if not (self.endpoint and self.secret):
            return None
        body = json.dumps({
            "type": "external",
            "url": url,
            "version": 2,
            "nsfw": self.nsfw_mode,
        }).encode()
        req = urllib.request.Request(
            f"{self.endpoint}/_metadata", data=body, method="POST",
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {self.secret}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except Exception:  # noqa: BLE001 -- metadata is best-effort, like Fluxer's
            return None


def _media_flags(meta):
    """media_proxy.rs::embed_media_flags."""
    flags = 0
    if meta.get("nsfw"):
        flags |= 1 << 4
    if meta.get("animated"):
        flags |= 1 << 5
    return flags


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #

class YouTubeClient:
    """Resolve YouTube links into Fluxer ``video`` embeds, with a disk cache.

    The cache is keyed by video id and also remembers *misses* (deleted or
    private videos), so re-runs cost no API quota. One ``videos.list`` call is
    1 quota unit against the default 10,000/day.
    """

    def __init__(self, api_key, media_proxy_endpoint=None, media_proxy_secret=None,
                 cache_path=None, verbose=False):
        self.api_key = api_key
        self.cache_path = cache_path
        self.verbose = verbose
        self.api_calls = 0
        self.media_proxy = MediaProxyClient(media_proxy_endpoint, media_proxy_secret)
        self.cache = {}
        if cache_path and os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    self.cache = json.load(f)
            except (OSError, ValueError):
                self.cache = {}

    def save_cache(self):
        if not self.cache_path:
            return
        tmp = f"{self.cache_path}.tmp"
        with open(tmp, "w") as f:
            json.dump(self.cache, f, ensure_ascii=False)
        os.replace(tmp, self.cache_path)

    def resolve_embed(self, url):
        """Return the embed for a YouTube URL, or None if it can't be resolved."""
        parsed = parse_youtube_url(url or "")
        if not parsed:
            return None
        video_id, start = parsed
        if video_id not in self.cache:
            self.cache[video_id] = self._build(video_id)
        embed = self.cache[video_id]
        if not embed:
            return None
        return _with_timestamp(copy.deepcopy(embed), video_id, start)

    def _build(self, video_id):
        video = self._fetch(video_id)
        if not video:
            return None
        snippet = video.get("snippet") or {}

        description = _clamp_lines(
            _truncate(_youtube_string(snippet.get("description")), DESCRIPTION_MAX),
            DESCRIPTION_LINES_MAX,
        )

        author = None
        channel_title = (snippet.get("channelTitle") or "").strip()
        if channel_title:
            author = {
                "name": _truncate(channel_title, AUTHOR_NAME_MAX),
                "url": f"https://www.youtube.com/channel/{snippet.get('channelId')}",
                "icon_url": None,
            }

        width, height = _embed_dimensions((video.get("player") or {}).get("embedHtml"))

        return {
            "type": "video",
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": _truncate(snippet.get("title") or "", TITLE_MAX),
            "description": description,
            "color": YOUTUBE_COLOR,
            "timestamp": None,
            "author": author,
            "provider": {"name": "YouTube", "url": PROVIDER_URL},
            "thumbnail": self._thumbnail(snippet.get("thumbnails") or {}),
            "image": None,
            "video": _media(f"https://www.youtube.com/embed/{video_id}",
                            width=width, height=height),
            "audio": None,
            "footer": None,
            "fields": None,
            "children": None,
            "nsfw": None,
            "html": None,
            "html_width": None,
            "html_height": None,
        }

    def _fetch(self, video_id):
        """videos.list -- returns the item, or None when the video is gone."""
        query = urllib.parse.urlencode({
            "key": self.api_key,
            "id": video_id,
            "part": "snippet,player,status",
        })
        req = urllib.request.Request(f"{YOUTUBE_API_BASE}?{query}")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as e:  # noqa: BLE001
            if self.verbose:
                print(f"  ! youtube api failed for {video_id}: {e}")
            return None
        finally:
            self.api_calls += 1
        items = data.get("items") or []
        return items[0] if items else None

    def _thumbnail(self, thumbnails):
        """youtube.rs: best() = maxres > high > standard > medium > default."""
        best = None
        for size in ("maxres", "high", "standard", "medium", "default"):
            if thumbnails.get(size):
                best = thumbnails[size]
                break
        if not best or not best.get("url"):
            return None
        meta = self.media_proxy.metadata(best["url"]) or {}
        return _media(
            best["url"],
            width=best.get("width") or meta.get("width"),
            height=best.get("height") or meta.get("height"),
            content_type=meta.get("content_type") or "image/jpeg",
            content_hash=meta.get("content_hash"),
            placeholder=meta.get("placeholder"),
            duration=int(meta["duration"]) if meta.get("duration") else None,
            flags=_media_flags(meta) if meta else 0,
        )


def _media(url, width=None, height=None, content_type=None, content_hash=None,
           placeholder=None, duration=None, flags=0):
    """An embed media object with the full key set Fluxer persists."""
    return {
        "url": url,
        "flags": flags,
        "width": width,
        "height": height,
        "duration": duration,
        "description": None,
        "placeholder": placeholder,
        "content_hash": content_hash,
        "content_type": content_type,
    }


def _with_timestamp(embed, video_id, start):
    """Re-point a cached embed at a ``?t=`` deep link (youtube.rs uses ``start``)."""
    if not start:
        return embed
    embed["url"] = f"https://www.youtube.com/watch?v={video_id}&start={start}"
    if embed.get("video"):
        embed["video"]["url"] = f"https://www.youtube.com/embed/{video_id}?start={start}"
    return embed
