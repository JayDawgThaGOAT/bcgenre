"""Fetch Bandcamp release tags and merge them into beets genres.

Resolves album pages from Bandcamp URLs stored in ``comments``, extracts raw
keywords (including the style pill beetcamp discards), normalizes them with
lastgenre helpers, filters against a MusicBrainz whitelist, and merges into
the ``genres`` multi-value field with lastgenre-compatible ``force`` /
``keep_existing`` semantics.

Default whitelist, aliases, and canonicalization tree are resolved from
lastgenre's shipped data files (always present with beets). Configuration
mirrors lastgenre's options under a separate ``bcgenre:`` block.
"""

from __future__ import annotations

import optparse
import re
from functools import singledispatchmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

import confuse
import yaml
from beets import library, plugins, ui
from beets.dbcore import types
from beets.importer import ImportTask
from beets.library import Album, Item, LibModel
from beetsplug.lastgenre import ALIASES_FILE, C14N_TREE, WHITELIST, flatten_tree

from beetsplug.bcgenre.genres import GenreResolver
from beetsplug.bcgenre.resolve import URL_FIELD, BandcampResolver, RateLimitAborted

if TYPE_CHECKING:
    from beets.importer import ImportSession
    from beetsplug.lastgenre.utils import AliasPatternWithReplacement

    CanonTree = list[list[str]]


class BCGenrePlugin(plugins.BeetsPlugin):
    album_types = {URL_FIELD: types.STRING}
    item_types = {URL_FIELD: types.STRING}

    def __init__(self) -> None:
        super().__init__()

        # Defaults mirror lastgenre's config.add() for the shared options.
        self.config.add(
            {
                "whitelist": True,
                "canonical": False,
                "count": 1,
                "fallback": None,
                "force": False,
                "keep_existing": False,
                "title_case": True,
                "auto": False,
                "pretend": False,
                "aliases": True,
            }
        )

        self.whitelist: set[str] = set()
        self.alias_patterns: list[AliasPatternWithReplacement] = []
        self.c14n_branches: CanonTree = []
        self.canonicalize = False
        self._resolver: BandcampResolver | None = None
        self._genres: GenreResolver | None = None

        self.register_listener("pluginload", self._setup)

        if self.config["auto"].get(bool):
            self.import_stages = [self.imported]

    # ------------------------------------------------------------------ config

    def _setup(self) -> None:
        """Load whitelist, aliases, and c14n tree after all plugins have loaded."""
        self.whitelist = self._load_whitelist()
        self.alias_patterns = self._load_aliases()
        self.c14n_branches, self.canonicalize = self._load_c14n_tree()
        self._resolver = BandcampResolver(
            self._log, pretend=bool(self.config["pretend"].get())
        )
        self._genres = GenreResolver(
            self._log,
            self.config,
            self._resolver,
            self.whitelist,
            self.alias_patterns,
            self.c14n_branches,
            self.canonicalize,
        )
        self._log.debug(
            "bcgenre ready: whitelist={} aliases={} canonicalize={} branches={}",
            len(self.whitelist),
            len(self.alias_patterns),
            self.canonicalize,
            len(self.c14n_branches),
        )

    def _ensure_setup(self) -> tuple[BandcampResolver, GenreResolver]:
        if self._resolver is None or self._genres is None:
            self._setup()
        assert self._resolver is not None and self._genres is not None
        # Keep pretend flag in sync after CLI set_args.
        self._resolver.pretend = bool(self.config["pretend"].get())
        return self._resolver, self._genres

    def _load_whitelist(self) -> set[str]:
        """Load the genre whitelist (mirrors lastgenre._load_whitelist)."""
        whitelist: set[str] = set()
        wl_filename = self.config["whitelist"].get()
        if wl_filename in (True, "", None):
            wl_filename = WHITELIST
        if wl_filename:
            self._log.debug("Loading whitelist {}", wl_filename)
            text = Path(wl_filename).expanduser().read_text(encoding="utf-8")
            for line in text.splitlines():
                if (line := line.strip().lower()) and not line.startswith("#"):
                    whitelist.add(line)
        return whitelist

    def _load_aliases(self) -> list[AliasPatternWithReplacement]:
        """Load genre aliases from ``bcgenre.aliases``.

        - ``yes`` (default): lastgenre's shipped ``aliases.yaml``
        - ``no``: disable alias normalization
        - mapping: replace the built-in table with the inline dict
        """
        aliases_config = self.config["aliases"].get()
        if aliases_config is False:
            return []

        aliases_view = confuse.Configuration(self.config["aliases"].name, read=False)
        if aliases_config in (True, "", None):
            self._log.debug("Loading aliases from lastgenre {}", ALIASES_FILE)
            with Path(ALIASES_FILE).open(encoding="utf-8") as f:
                aliases_view.set(yaml.safe_load(f))
        elif not isinstance(aliases_config, dict):
            raise confuse.ConfigTypeError(
                f"{self.config['aliases'].name} must be a dict or bool."
            )
        else:
            aliases_view.set(aliases_config)

        raw_aliases = aliases_view.get(confuse.MappingValues(confuse.Sequence(str)))
        compiled: list[AliasPatternWithReplacement] = []
        for canonical, patterns in raw_aliases.items():
            lower_canonical = canonical.lower()
            compiled.extend(
                (re.compile(p, re.IGNORECASE), lower_canonical) for p in patterns
            )
        self._log.debug("Loaded {} alias entries", len(compiled))
        return compiled

    def _load_c14n_tree(self) -> tuple[CanonTree, bool]:
        """Load lastgenre's genres-tree.yaml when ``bcgenre.canonical`` is enabled."""
        c14n_filename = self.config["canonical"].get()
        canonicalize = c14n_filename is not False
        if c14n_filename in (True, "", None):
            c14n_filename = C14N_TREE
        branches: CanonTree = []
        if canonicalize and c14n_filename:
            self._log.debug("Loading canonicalization tree {}", c14n_filename)
            with Path(c14n_filename).expanduser().open(encoding="utf-8") as f:
                genres_tree = yaml.safe_load(f)
            flatten_tree(genres_tree, [], branches)
        return branches, canonicalize

    # -------------------------------------------------------------- process

    def _as_genre_list(self, value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [str(v) for v in value]
        return [str(value)]

    def _fetch_and_log_genre(self, obj: LibModel) -> bool:
        """Fetch genre, log the +/- diff. Return False if the object was skipped."""
        _, genres = self._ensure_setup()
        self._log.info("{}", obj)
        result, label = genres.get_genre(obj)
        if result is None:
            self._log.debug("Skipped ({}): {}", label, obj)
            return False

        old = obj.copy()
        # Normalize missing genres to [] so beets uses the multi-value +/- form.
        old["genres"] = self._as_genre_list(old.get("genres"))
        obj["genres"] = result
        self._log.debug("Resolved ({}): {}", label, result)
        ui.show_model_changes(obj, old, fields=["genres"], print_obj=False)
        return True

    @singledispatchmethod
    def _process(self, obj: LibModel, write: bool) -> None:
        raise NotImplementedError

    @_process.register
    def _process_track(self, obj: Item, write: bool) -> None:
        if not self._fetch_and_log_genre(obj):
            return
        if not self.config["pretend"].get():
            obj.try_sync(write=write, move=False)

    @_process.register
    def _process_album(self, obj: Album, write: bool) -> None:
        # Resolve once at album level so the URL/cache is shared, then emit a
        # per-track +/- diff for every item.
        resolver, _genres = self._ensure_setup()
        try:
            url = resolver.resolve_url(obj)
        except RateLimitAborted:
            raise
        except Exception as exc:
            self._log.error("URL resolution failed for {}: {}", obj, exc)
            return

        if url:
            resolver.store_url(obj, url)

        if not self._fetch_and_log_genre(obj):
            for item in obj.items():
                self._process(item, write)
            return

        if not self.config["pretend"].get():
            obj.try_sync(write=write, move=False, inherit=True)

        for item in obj.items():
            old = item.copy()
            old["genres"] = self._as_genre_list(old.get("genres"))
            item["genres"] = list(obj.get("genres") or [])
            self._log.info("{}", item)
            ui.show_model_changes(item, old, fields=["genres"], print_obj=False)
            if not self.config["pretend"].get():
                item.try_sync(write=write, move=False)

    # -------------------------------------------------------------- CLI

    def commands(self) -> list[ui.Subcommand]:
        bcgenre_cmd = ui.Subcommand("bcgenre", help="fetch genres from Bandcamp")
        bcgenre_cmd.parser.add_option(
            "-p",
            "--pretend",
            action="store_true",
            help="show actions but do nothing",
        )
        bcgenre_cmd.parser.add_option(
            "-f",
            "--force",
            dest="force",
            action="store_true",
            help="modify existing genres",
        )
        bcgenre_cmd.parser.add_option(
            "-F",
            "--no-force",
            dest="force",
            action="store_false",
            help="don't modify existing genres",
        )
        bcgenre_cmd.parser.add_option(
            "-k",
            "--keep-existing",
            dest="keep_existing",
            action="store_true",
            help="combine with existing genres when modifying",
        )
        bcgenre_cmd.parser.add_option(
            "-K",
            "--no-keep-existing",
            dest="keep_existing",
            action="store_false",
            help="don't combine with existing genres when modifying",
        )
        bcgenre_cmd.parser.add_option(
            "-A",
            "--items",
            action="store_false",
            dest="album",
            help="match items instead of albums",
        )
        bcgenre_cmd.parser.add_option(
            "-a",
            "--albums",
            action="store_true",
            dest="album",
            help="match albums instead of items (default)",
        )
        bcgenre_cmd.parser.set_defaults(album=True)

        def bcgenre_func(
            lib: library.Library, opts: optparse.Values, args: list[str]
        ) -> None:
            self.config.set_args(opts)
            method = lib.albums if opts.album else lib.items
            try:
                for obj in method(args):
                    self._process(obj, write=ui.should_write())
            except RateLimitAborted as exc:
                raise ui.UserError(str(exc)) from exc

        bcgenre_cmd.func = bcgenre_func
        return [bcgenre_cmd]

    def imported(self, _: ImportSession, task: ImportTask) -> None:
        if not self.config["auto"].get(bool):
            return
        target = task.album if task.is_album else task.item
        if target is None:
            return
        try:
            self._process(target, write=False)
        except RateLimitAborted as exc:
            self._log.error("{}", exc)
