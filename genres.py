"""Bandcamp tag extraction, normalization, and genre merge logic."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from beetcamp import DEFAULT_CONFIG
from beetcamp.metaguru import Metaguru
from beets.library import LibModel

from beetsplug.lastgenre import find_parents
from beetsplug.lastgenre.utils import normalize_genre

from beetsplug.bcgenre.resolve import BandcampResolver, RateLimitAborted

if TYPE_CHECKING:
    from beets.logging import BeetsLogger
    from beetsplug.lastgenre.utils import AliasPatternWithReplacement
    from confuse import Subview

    CanonTree = list[list[str]]

TAG_SPLIT_RE = re.compile(r"[/,]")


class GenreResolver:
    """Extract and merge Bandcamp genres onto library items/albums."""

    def __init__(
        self,
        log: BeetsLogger,
        config: Subview,
        resolver: BandcampResolver,
        whitelist: set[str],
        alias_patterns: list[AliasPatternWithReplacement],
        c14n_branches: CanonTree,
        canonicalize: bool,
    ) -> None:
        self._log = log
        self.config = config
        self.resolver = resolver
        self.whitelist = whitelist
        self.alias_patterns = alias_patterns
        self.c14n_branches = c14n_branches
        self.canonicalize = canonicalize

    def _split_tags(self, raw: list[str]) -> list[str]:
        """Basic cleanup: lowercase, strip #, &→and, split on / and ,."""
        out: list[str] = []
        for kw in raw:
            cleaned = str(kw).strip().strip("#").replace("&", "and")
            for part in TAG_SPLIT_RE.split(cleaned):
                p = part.strip().lower()
                if p:
                    out.append(p)
        return out

    def _whitelist_match(self, tag: str) -> str | None:
        """Return the whitelist's canonical spelling, trying hyphen/space variants."""
        if not self.whitelist:
            return tag
        candidates = {tag, tag.replace("-", " "), tag.replace(" ", "-")}
        candidates |= {re.sub(r"\s+", " ", c) for c in candidates}
        candidates |= {re.sub(r"-+", "-", c) for c in candidates}
        for cand in candidates:
            if cand in self.whitelist:
                return cand
        return None

    def _normalize_tags(self, raw: list[str]) -> list[str]:
        """Cleanup → aliases → whitelist (with hyphen/space variants)."""
        tags = self._split_tags(raw)
        if self.alias_patterns:
            tags = [
                normalize_genre(self._log, self.alias_patterns, tag) for tag in tags
            ]

        matched: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            canon = self._whitelist_match(tag)
            if canon and canon not in seen:
                seen.add(canon)
                matched.append(canon)
        return matched

    def _apply_canonical(self, tags: list[str]) -> list[str]:
        """Optionally append whitelisted parent genres from the c14n tree."""
        if not self.canonicalize or not tags:
            return tags
        count = self.config["count"].get(int)
        expanded: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            parents = find_parents(tag, self.c14n_branches)
            for parent in parents:
                p = parent.lower()
                if self.whitelist and p not in self.whitelist:
                    continue
                if p not in seen:
                    seen.add(p)
                    expanded.append(p)
            if len(expanded) >= count:
                break
        return expanded or tags

    def _format_genres(self, tags: list[str]) -> list[str]:
        if self.config["title_case"].get():
            return [tag.title() for tag in tags]
        return tags

    def _extract_raw_tags(self, html: str) -> list[str]:
        """Parse Bandcamp HTML and return raw keywords + style."""
        guru = Metaguru.from_html(html, DEFAULT_CONFIG.copy())
        raw: list[str] = list(guru.meta.get("keywords") or [])
        style = guru.style
        if style and style not in raw:
            raw.append(style)
        return raw

    def genres_from_url(self, url: str) -> list[str]:
        html = self.resolver.fetch(url)
        raw = self._extract_raw_tags(html)
        tags = self._normalize_tags(raw)
        tags = self._apply_canonical(tags)
        count = self.config["count"].get(int)
        return self._format_genres(tags[:count])

    def existing_genres(self, obj: LibModel) -> list[str]:
        genres = obj.get("genres")
        if isinstance(genres, list):
            return [str(g) for g in genres if g]
        if genres:
            return [str(genres)]
        genre = obj.get("genre")
        if not genre:
            return []
        return [g.strip() for g in str(genre).split(",") if g.strip()]

    def get_genre(self, obj: LibModel) -> tuple[list[str] | None, str]:
        """Resolve genres for ``obj``.

        Returns ``(genres, label)``. ``genres`` is None when the object should
        be skipped (no Bandcamp URL / fetch failure). Empty list means "clear
        or keep nothing" depending on merge rules.
        """
        existing = self.existing_genres(obj)

        if existing and not self.config["force"].get():
            return existing, "keep any, no-force"

        try:
            url = self.resolver.resolve_url(obj)
        except RateLimitAborted:
            raise
        except Exception as exc:
            self._log.error("URL resolution failed for {}: {}", obj, exc)
            return None, "error"

        if not url:
            self._log.debug("No Bandcamp URL for {}", obj)
            fallback = self.config["fallback"].get()
            if fallback and (self.config["force"].get() or not existing):
                return [str(fallback)], "fallback"
            if existing:
                return existing, "keep any, no-url"
            return None, "no-url"

        try:
            new_genres = self.genres_from_url(url)
        except RateLimitAborted:
            raise
        except LookupError:
            self._log.warning("Bandcamp page not found: {}", url)
            return None, "404"
        except Exception as exc:
            self._log.error("Failed to extract genres from {}: {}", url, exc)
            return None, "error"

        self.resolver.store_url(obj, url)

        if self.config["force"].get() and self.config["keep_existing"].get():
            seen = {g.lower() for g in existing}
            keep = list(existing)
            for g in new_genres:
                if g.lower() not in seen:
                    keep.append(g)
                    seen.add(g.lower())
            return keep, "keep + bandcamp, whitelist"

        if new_genres:
            return new_genres, "bandcamp, whitelist"

        fallback = self.config["fallback"].get()
        if fallback:
            return [str(fallback)], "fallback"
        if existing and self.config["keep_existing"].get():
            return existing, "keep any, empty-bandcamp"
        return [], "bandcamp empty"
