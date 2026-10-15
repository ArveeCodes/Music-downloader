# Kraken — Changes & Context

## Project Overview

- **Repo**: `ArveeCodes/spotify-downloader`, branch `main` (built on spotDL v4.5.0)
- **Stack**: Python 3.10+, FastAPI, Jinja2 templates, Datastar (reactive SSE framework), Tailwind CSS 4, DaisyUI 5 (`night` theme), inline SVG icons via `icons.html.j2` (Iconify is still loaded for templates that use `<iconify-icon>`)
- **Downloads**: saved to `E:\Music Downloader\downloads` (set in `~/.spotdl/config.json`, see README)
- **Working dir**: `E:\Music Downloader` (this IS the repo root — no subfolder)
- **Entry point**: `spotdl web` starts the FastAPI server via `spotdl/console/web.py`
- **Frontend**: Server-rendered Jinja2 templates in `spotdl/web/components/`, reactive updates via Datastar SSE patches
- **API**: REST endpoints in `spotdl/web/api.py`, SSE routes in `spotdl/web/routes.py`

---

## Latest: navy redesign, live link downloads, downloads folder

### UI
- **Name**: the app is now called **Kraken**. The wordmark (`.wordmark` in `base.html.j2`) is uppercase with wide tracking and a white-to-blue gradient. This is the display name only; the Python package, the commands (`spotdl ...`) and the config folder (`~/.spotdl`) are unchanged.
- **Navy theme**: all colors are CSS variables in `base.html.j2` (`--bg #08142b`, `--surface`, `--accent #5b9bff`, ...), with DaisyUI's `night` theme underneath so built-in components match. A static glow sits at the top of the page; there are no animated backgrounds or backdrop blur.
- **Icons**: inline SVGs from the `icon()` macro in `icons.html.j2`, so there are no runtime icon fetches.
- **Logo and favicon**: navy tile with blue equalizer bars and a white download arrow; the SVG doubles as the favicon.
- **Home**: tagline "Paste a link. Keep the music." plus a three-step explainer.
- **Search results**: grouped into Artists, Albums and Songs panels.
- **Footer**: links to this repo and credits upstream spotDL.

### Link downloads (the old redirect is gone)
- Pasting a Spotify link used to redirect to `/downloads`. `handle_get_client_search` now streams an inline card: resolving, then a live percentage, then done, partial or failed. Collections also show "x of y".
- `background_download` runs the batch as a task, so it keeps going if the page closes, and it pins the client session while running.
- Submitting the same link again attaches to the running download instead of starting a duplicate.

### Search
- `quick_search` builds songs, albums and artists from one `searchDesktop` call (previously about three API calls per track). It still goes through `_cached_search` / `_deduped_search`.

### Files and folders
- Downloads are saved to `E:\Music Downloader\downloads` (`web_use_output_dir: true` with an absolute `output` path in the config).
- Albums, playlists and artists get a subfolder named after the collection (`_output_with_subfolder`, with unsafe characters stripped).
- `Downloader.search_and_download(song, output_template)` takes the destination per call, so concurrent downloads can no longer redirect each other through shared settings.

### Reliability and security
- `Client` registers itself before its slow initialization, exposes an `_initialized` flag, and ref-counts its streams, so a late-closing stream can't remove a session that's still in use.
- `client_id` must be hex (it's used in file paths), and `handle_signals` validates and sanitizes it.
- Jinja autoescape is on, and download buttons use hashed IDs (`safe_id`) instead of raw URLs.
- `wait_for_client` covers the race between `/client/load` and the other endpoints.

### Performance
- `utils/spotapi_cache.py` shares one Spotify session across requests and persists its query hashes to `~/.spotdl/spotapi-hashes.json`. Link resolution dropped from about 60s to about 1s.
- The navbar badge and the downloads list only send a patch when something changed. The rotating search placeholder is now client-side JS (its SSE endpoint was removed).
- `api.py` `songs_from_url` uses `fetch_songs=False` instead of re-fetching every track.

### File map (this round)

```
spotdl/
├── download/downloader.py         # Per-call output_template
├── utils/
│   ├── spotapi_cache.py           # New: shared Spotify session + persisted hashes
│   ├── spotify.py                 # Installs the session cache
│   └── web.py                     # Client init flag, ref-counted streams, id validation
└── web/
    ├── api.py                     # Faster songs_from_url
    ├── routes.py                  # Inline link downloads, quick_search, helpers
    ├── utils.py                   # Signal validation
    ├── static/assets/spotdl.svg   # New logo / favicon
    └── components/                # Navy redesign; new icons.html.j2;
                                   # search-input-rotating-placeholder removed
```

Code paths not listed here were left unchanged.

---

## Backend Efficiency Fixes

### 1. ProgressTracker shared state bug (CRITICAL)
**File**: `spotdl/download/progress_handler.py` line ~115

`ProgressTracker.songs` was a **class variable** (`songs: Dict = {}`), meaning every `ProgressTracker` instance across all clients shared the same dict. Client A could see Client B's downloads.

**Fix**: Changed to instance variable via `__init__`:
```python
class ProgressTracker:
    def __init__(self):
        self.songs: Dict[str, ClientSongDownload] = {}
```

### 2. Redundant AudioProvider instantiation per song
**File**: `spotdl/download/downloader.py` around line 653

`search_and_download()` created a brand new `AudioProvider` or `Piped` instance for every single song download. Each instantiation spins up a fresh `YoutubeDL` object — expensive. The `Downloader.__init__` already creates `self.audio_providers` list.

**Fix**: Reuse `self.audio_providers[0]` instead of constructing a new one. Added `_progress_hooks.clear()` before adding the per-song hook to prevent hook accumulation on the shared instance.

### 3. Cache-Control header typo
**File**: `spotdl/utils/web.py` line ~64

Had `"max-age=0, no-cache, no-store, , must-revalidate"` (double comma). Fixed to `"max-age=0, no-cache, no-store, must-revalidate"`.

### 4. SSE polling intervals too aggressive
**File**: `spotdl/web/routes.py`

- `handle_get_client_load` polled every 1s for completed task count → changed to 3s
- `handle_get_client_downloads` polled every 1s for download list → changed to 2s

### 5. Dead code cleanup
- `spotdl/web/api.py`: Removed ~38 lines of commented-out websocket endpoint code
- `spotdl/utils/web.py`: Removed dead websocket-era comments from `Client` class
- `spotdl/web/routes.py`: Cleaned unused imports (`Any`, commented import lines)

---

## Frontend Improvements

### 1. Home page search bar
**File**: `spotdl/web/components/home.html.j2`

Old home page was just a hero section with logo + info alert. Now has:
- Full-width search input with "Paste a Spotify URL or search for a song..." placeholder
- Search button with icon
- Content type badges (Songs, Albums, Playlists, Artists)
- Uses `data-signals="{home_search: ''}"` for Datastar binding
- Enter key triggers search via `goto('/search?q=' + encodeURIComponent($home_search))`

### 2. Mobile navbar search
**File**: `spotdl/web/components/navbar.html.j2`

Old navbar search had `class="hidden sm:flex"` — completely invisible on mobile. Now:
- Search bar visible at all screen sizes
- All buttons use `btn-sm sm:btn-md` for responsive sizing
- Logo text hidden on mobile (`hidden sm:inline`) to save space

### 3. Theme switcher
**File**: `spotdl/web/components/navbar.html.j2`

Was commented out. Now implemented as a swap toggle with sun/moon icons:
```html
<label class="swap swap-rotate btn btn-ghost btn-circle">
    <input type="checkbox" data-on-change="document.documentElement.setAttribute('data-theme', evt.target.checked ? 'light' : 'forest')" />
    <iconify-icon icon="clarity:sun-line" class="swap-on" />
    <iconify-icon icon="clarity:moon-line" class="swap-off" />
</label>
```

### 4. Download list redesign
**File**: `spotdl/web/components/download-list.html.j2`

Old list was basic cards with minimal status. New version:
- Each download has album art thumbnail (avatar), song name, artist, album
- Status badges: error (red), done (green), processing (blue spinner), downloading (yellow + progress bar + percentage)
- Empty state has a "Search" button shortcut back to home
- Responsive — status badges hide on mobile, progress bar resizes

### 5. Search results cleanup
**File**: `spotdl/web/components/search-list.html.j2`

- Removed unused inline `<script>` with dead `download()` function (wasn't used — Datastar handles downloads via `data-on-click`)
- Added `loading="lazy"` on album art images
- Added `rel="noopener noreferrer"` on external Spotify links

### 6. Settings modal fix
**File**: `spotdl/web/components/settings-content.html.j2`

- Format dropdown was checking `downloader_settings.formats` (doesn't exist) instead of `downloader_settings.format` — fixed
- Added `cursor-pointer` to checkbox labels
- Removed commented-out save button and spotify settings code
- Removed `console.log` from checkbox change handlers

### 7. Base layout fix
**File**: `spotdl/web/components/base.html.j2`

- Changed from `absolute w-full` to `min-h-screen flex flex-col` so footer sticks to bottom properly
- Content area wrapped in `flex-1` div
- Added toast container div for future notification support
- Removed commented-out debug signals display
- Added `min-height: 100vh` to html/body

---

## File Map (what was changed)

```
spotdl/
├── download/
│   ├── downloader.py          # Reuse audio providers, clear hooks
│   └── progress_handler.py    # Fix class variable → instance variable
├── utils/
│   └── web.py                 # Cache-Control fix, dead code cleanup
└── web/
    ├── api.py                 # Remove dead websocket code
    ├── routes.py              # Reduce polling, clean imports
    ├── utils.py               # (unchanged)
    └── components/
        ├── base.html.j2               # Flexbox layout, toast container
        ├── home.html.j2               # Search bar added
        ├── navbar.html.j2             # Mobile search, theme switcher
        ├── search.html.j2             # Cleaned up
        ├── search-list.html.j2        # Removed dead script
        ├── downloads.html.j2          # (unchanged — just wrapper)
        ├── download-list.html.j2      # Full redesign
        ├── footer.html.j2             # Added aria labels, responsive padding
        ├── settings.html              # Responsive button
        ├── settings-content.html.j2   # Format fix, cleanup
        ├── search-input-rotating-placeholder.html.j2  # (unchanged)
        └── status-disconnected.html.j2                # (unchanged)
```

---

## What Still Needs Work

Resolved in the latest round and removed from this list: the theme-reset bug (there's a single theme now), the rotating-placeholder SSE loop, the slow per-track `songs_from_url`, and the single-song-only downloads (album, playlist and artist rows download whole collections).

- **No error toasts**: the toast container exists but nothing uses it. Errors currently show as inline cards on the search page.
- **`web.py` console module**: creates a `ProactorEventLoop` on Windows, which is deprecated in newer Python.
- **No multi-select**: songs in search results still download one at a time.
- **Iconify**: still loaded for templates that use `<iconify-icon>`. Converting those to the `icon()` macro would let the script be dropped.
- **Temporary-folder mode**: with `web_use_output_dir` off, files are saved one at a time from the Downloads page, so album folders don't survive. A zip download per collection would fix that.
- **Tests**: `tests/utils/test_config.py` (5 tests, Windows path handling), `tests/console/test_entry_point.py::test_preload_song`, `tests/test_init.py::test_download` (its source video is no longer available) and 2 setup errors in `test_downloader.py` / `test_spotify.py` fail. They fail the same way on the commit before the latest round, so they're pre-existing. The web layer has no automated tests.
