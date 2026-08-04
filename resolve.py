"""Bandcamp URL resolution and paced HTTP fetching."""

from __future__ import annotations

import re
import time
from html import unescape
from typing import TYPE_CHECKING
from urllib.parse import urljoin

import httpx
from beetcamp.http import USER_AGENT, urlify
from beetcamp.search import get_similarity
from beets.library import Album, Item, LibModel

if TYPE_CHECKING:
    from beets.logging import BeetsLogger

# Fixed, non-configurable pacing (seconds).
MIN_FETCH_INTERVAL = 1.0
MAX_429_BACKOFF = 8.0
MAX_CONSECUTIVE_429 = 5

URL_FIELD = "bandcamp_album_url"

BANDCAMP_ROOT_RE = re.compile(
    r"https?://(?:www\.)?([\w-]+\.bandcamp\.com)(?:/(album|track)/([\w-]+))?",
    re.IGNORECASE,
)
MUSIC_PAIR_RE = re.compile(
    r'href="(/(?:album|track)/[\w-]+)"[\s\S]{0,400}?class="title"[^>]*>\s*([^<]+)',
    re.IGNORECASE,
)


class RateLimitAborted(RuntimeError):
    """Raised when Bandcamp returns too many consecutive HTTP 429 responses."""


class BandcampResolver:
    """Resolve Bandcamp album/track URLs and fetch pages with rate limiting."""

    def __init__(self, log: BeetsLogger, pretend: bool = False) -> None:
        self._log = log
        self.pretend = pretend
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=30.0,
        )
        self._last_fetch = 0.0
        self._consecutive_429 = 0
        self._page_cache: dict[str, str] = {}
        self._music_cache: dict[str, list[tuple[str, str]]] = {}

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ HTTP

    def _throttle(self) -> None:
        """Sleep until MIN_FETCH_INTERVAL has elapsed since the last fetch."""
        elapsed = time.monotonic() - self._last_fetch
        if elapsed < MIN_FETCH_INTERVAL:
            time.sleep(MIN_FETCH_INTERVAL - elapsed)

    def fetch(self, url: str) -> str:
        """GET ``url`` with pacing, 429 backoff, and an in-process page cache."""
        if url in self._page_cache:
            return self._page_cache[url]

        backoff = 1.0
        while True:
            self._throttle()
            self._last_fetch = time.monotonic()
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                raise RuntimeError(f"HTTP error fetching {url}: {exc}") from exc

            if response.status_code == 429:
                self._consecutive_429 += 1
                if self._consecutive_429 >= MAX_CONSECUTIVE_429:
                    raise RateLimitAborted(
                        f"Bandcamp returned {MAX_CONSECUTIVE_429} consecutive "
                        "HTTP 429 responses; aborting to avoid a lockout"
                    )
                retry_after = response.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else backoff
                except ValueError:
                    wait = backoff
                wait = min(max(wait, MIN_FETCH_INTERVAL), MAX_429_BACKOFF * 2)
                self._log.warning(
                    "HTTP 429 from Bandcamp; sleeping {:.1f}s ({}/{})",
                    wait,
                    self._consecutive_429,
                    MAX_CONSECUTIVE_429,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, MAX_429_BACKOFF)
                continue

            self._consecutive_429 = 0
            if response.status_code == 404:
                raise LookupError(f"404 {url}")
            response.raise_for_status()
            text = unescape(response.text)
            self._page_cache[url] = text
            return text

    def _head_ok(self, url: str) -> bool:
        """Return True if ``url`` responds with HTTP 200 (paced)."""
        if url in self._page_cache:
            return True
        self._throttle()
        self._last_fetch = time.monotonic()
        try:
            response = self._client.head(url)
            if response.status_code in {405, 429}:
                self.fetch(url)
                return True
            return response.status_code == 200
        except RateLimitAborted:
            raise
        except (httpx.HTTPError, RuntimeError, LookupError):
            return False

    # ---------------------------------------------------------- URL resolution

    @staticmethod
    def bandcamp_root_from_comments(
        comments: str,
    ) -> tuple[str | None, str | None]:
        """Return (root_url, deep_album_or_track_url) extracted from comments."""
        if not comments:
            return None, None
        match = BANDCAMP_ROOT_RE.search(comments)
        if not match:
            return None, None
        host = match.group(1).lower()
        root = f"https://{host}"
        kind, slug = match.group(2), match.group(3)
        if kind == "album" and slug:
            return root, f"{root}/album/{slug}"
        if kind == "track" and slug:
            return root, f"{root}/track/{slug}"
        return root, None

    @staticmethod
    def guess_album_url(root: str, album: str) -> str:
        return f"{root.rstrip('/')}/album/{urlify(album)}"

    def scrape_music(self, root: str) -> list[tuple[str, str]]:
        """Return (path, title) pairs from ``{root}/music``."""
        root = root.rstrip("/")
        if root in self._music_cache:
            return self._music_cache[root]
        html = self.fetch(f"{root}/music")
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for path, title in MUSIC_PAIR_RE.findall(html):
            title = unescape(title).strip()
            if not title or path in seen:
                continue
            seen.add(path)
            pairs.append((path, title))
        self._music_cache[root] = pairs
        return pairs

    def match_music(
        self, root: str, album: str, min_similarity: float = 0.55
    ) -> str | None:
        """Fuzzy-match ``album`` against the artist's /music discography."""
        best_url: str | None = None
        best_sim = 0.0
        for path, title in self.scrape_music(root):
            if not path.startswith("/album/"):
                continue
            sim = get_similarity(album, title)
            if sim > best_sim:
                best_sim = sim
                best_url = urljoin(root + "/", path.lstrip("/"))
        if best_url and best_sim >= min_similarity:
            self._log.debug(
                "Matched {!r} -> {} (similarity={:.3f})", album, best_url, best_sim
            )
            return best_url
        return None

    def cached_url(self, obj: LibModel) -> str | None:
        url = obj.get(URL_FIELD)
        return str(url) if url else None

    def resolve_url(self, obj: LibModel) -> str | None:
        """Resolve a Bandcamp album/track URL for ``obj``."""
        if cached := self.cached_url(obj):
            return cached

        if isinstance(obj, Item):
            album = obj.get("album") or ""
            comments = obj.get("comments") or ""
            if obj.album_id is not None:
                try:
                    alb = obj.get_album()
                except Exception:
                    alb = None
                if alb and (cached := self.cached_url(alb)):
                    return cached
        else:
            album = obj.get("album") or ""
            items = list(obj.items())
            comments = (items[0].get("comments") or "") if items else ""
            comments = obj.get("comments") or comments

        root, deep = self.bandcamp_root_from_comments(comments)
        if deep:
            return deep
        if not root:
            return None

        if album:
            guessed = self.guess_album_url(root, album)
            try:
                if self._head_ok(guessed):
                    return guessed
            except RateLimitAborted:
                raise
            except Exception as exc:
                self._log.debug("Slug guess failed for {}: {}", guessed, exc)

            try:
                if matched := self.match_music(root, album):
                    return matched
            except RateLimitAborted:
                raise
            except Exception as exc:
                self._log.debug("Music scrape failed for {}: {}", root, exc)

        return None

    def store_url(self, obj: LibModel, url: str) -> None:
        """Cache the resolved URL on the album (and item when processing tracks).

        Always set the field in memory so later steps in the same run reuse it.
        Persistence is skipped under ``pretend``.
        """
        obj[URL_FIELD] = url
        if self.pretend:
            return
        try:
            if isinstance(obj, Item):
                obj.store()
                if alb := obj.get_album():
                    if not alb.get(URL_FIELD):
                        alb[URL_FIELD] = url
                        alb.store()
            else:
                obj.store(inherit=False)
        except Exception as exc:
            self._log.debug("Could not cache {}: {}", URL_FIELD, exc)
