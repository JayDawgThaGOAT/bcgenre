# bcgenre

A [beets](https://beets.io/) plugin that fetches genre tags from Bandcamp release pages and merges them into your library’s `genres` field.

It complements [`lastgenre`](https://beets.readthedocs.io/en/stable/plugins/lastgenre.html): Last.fm often misses artist-authored Bandcamp tags (and Bandcamp’s hyphenated forms like `hip-hop` / `neo-soul`). `bcgenre` reads those tags from the release page, normalizes them with lastgenre’s helpers, and filters them against a MusicBrainz genre whitelist.

**PyPI:** [beets-bcgenre](https://pypi.org/project/beets-bcgenre/)  
**Source:** [JayDawgThaGOAT/bcgenre](https://github.com/JayDawgThaGOAT/bcgenre)

## Requirements

- [beets](https://beets.io/) ≥ 2.0 (includes the `lastgenre` package used for whitelist / aliases / canonicalization data)
- [beetcamp](https://github.com/snejus/beetcamp) (`bandcamp` plugin) for Bandcamp HTML parsing and slug helpers — pulled in automatically via PyPI
- A Bandcamp URL in each item’s `comments` field (artist root or album URL), e.g. `Visit https://artist.bandcamp.com`

## Install

```bash
pip install beets-bcgenre
```

Enable the plugin (and usually `lastgenre` + `bandcamp`):

```yaml
plugins: lastgenre bandcamp bcgenre
```

Confirm it loads:

```bash
beet bcgenre -h
```

### From source / pluginpath

For development, clone the repo so beets can see `beetsplug.bcgenre`, then point `pluginpath` at the parent of that `beetsplug` directory:

```yaml
plugins: lastgenre bandcamp bcgenre
pluginpath: /path/to/parent   # directory that contains beetsplug/
```

## Configuration

Options live under `bcgenre:` and mirror lastgenre’s shared settings (they do **not** inherit from your `lastgenre:` block):

```yaml
bcgenre:
    auto: no
    force: no
    keep_existing: no
    count: 1
    whitelist: yes          # yes = lastgenre's genres.txt; or a path to your own list
    aliases: yes            # yes = lastgenre's aliases.yaml; no = off; or an inline mapping
    canonical: no           # yes = add parent genres from lastgenre's genres-tree.yaml
    title_case: yes
    fallback:               # optional genre when Bandcamp yields nothing
    pretend: no
```

| Option | Meaning |
| --- | --- |
| `force` | Modify items that already have genres |
| `keep_existing` | When forcing, union Bandcamp genres with existing ones |
| `count` | Max genres kept from Bandcamp after filtering (union with `keep_existing` can exceed this) |
| `whitelist` | Genre allow-list; `yes` uses lastgenre’s bundled MusicBrainz list |
| `aliases` | Spelling normalization (`hip-hop` → `hip hop`, etc.) |
| `canonical` | Expand matched tags with parent genres from the genre tree |
| `auto` | Run during import (off by default; prefer batch runs) |
| `title_case` | Title-case written genres |

File writes follow your global `import.write` setting (same as `lastgenre`).

## Usage

```bash
# Preview changes (no DB / file writes)
beet bcgenre -p

# Apply (uses config force / keep_existing)
beet bcgenre

# Force + replace existing genres
beet bcgenre -f -K

# Force + union with existing genres
beet bcgenre -f -k

# Limit to a query (albums by default)
beet bcgenre -p albumartist:Aesop\ Rock
beet bcgenre -A artist:Aesop\ Rock    # items instead of albums
```

CLI flags match lastgenre: `-p/--pretend`, `-f/-F` (force), `-k/-K` (keep-existing), `-a/-A` (albums/items).

Output shows the same per-track `genres:` `+` / `-` diffs as `lastgenre`.

## How URL resolution works

For each album, bcgenre:

1. Reuses a cached `bandcamp_album_url` flex field if present
2. Uses a deep `/album/...` (or `/track/...`) URL from `comments` when available
3. Otherwise takes the Bandcamp root from `comments` and guesses `/album/{slug}`
4. On miss, scrapes `{root}/music` and fuzzy-matches the album title (handles Bandcamp’s `-2` slug collisions)

Resolved URLs are stored in `bandcamp_album_url` so re-runs avoid most network work.

Requests are paced at about 1 per second (not configurable), with backoff and abort on repeated HTTP 429 responses.

## Tips

- Prefer a pretend pass first: `beet bcgenre -p -f -k`
- Albums without a Bandcamp URL in `comments` are skipped
- Translated or heavily renamed titles may not match Bandcamp’s discography listing; set `bandcamp_album_url` manually or restore a closer title
- `lastgenre` does not need to be enabled for bcgenre to use its data files, but enabling both is the usual setup
