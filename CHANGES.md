# spotDL Web UI — Changes & Context

## Project Overview

- **Repo**: `spotDL/spotify-downloader` (v4.5.0)
- **Stack**: Python 3.10+, FastAPI, Jinja2 templates, Datastar (reactive SSE framework), Tailwind CSS 4, DaisyUI 5, Iconify icons
- **Working dir**: `E:\Music Downloader` (this IS the repo root — no subfolder)
- **Entry point**: `spotdl web` starts the FastAPI server via `spotdl/console/web.py`
- **Frontend**: Server-rendered Jinja2 templates in `spotdl/web/components/`, reactive updates via Datastar SSE patches
- **API**: REST endpoints in `spotdl/web/api.py`, SSE routes in `spotdl/web/routes.py`

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

- **No persistent theme**: Theme switcher resets on page reload — needs localStorage persistence
- **Rotating placeholder SSE**: The search placeholder rotation runs an infinite SSE loop just to cycle 6 strings — could be pure client-side JS
- **`songs_from_url` in api.py**: Does sequential `Song.from_url` per playlist track — could parallelize with asyncio
- **No error toasts**: Toast container div exists but nothing populates it yet
- **`validate_search_term` in utils/web.py**: Only accepts Spotify URLs — doesn't handle YouTube URLs or plain search queries that spotdl supports via CLI
- **`web.py` console module**: Creates `ProactorEventLoop` on Windows which is deprecated in newer Python
- **No batch download**: Can only download one song at a time from search results
- **Tests**: No changes to test files — existing tests in `tests/` should still pass but worth verifying
