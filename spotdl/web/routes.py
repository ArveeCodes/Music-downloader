"""
Module which contains the web client routes and functions.
"""

import asyncio
import hashlib
import time
import uuid
from html import escape as _esc
from pathlib import Path
from typing import Any, Dict, Optional, cast

from datastar_py.fastapi import ReadSignals
from datastar_py.fastapi import ServerSentEventGenerator as SSE
from datastar_py.fastapi import datastar_response
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from spotdl._version import __version__
from spotdl.download.downloader import AUDIO_PROVIDERS, LYRICS_PROVIDERS
from spotdl.types.album import Album
from spotdl.types.artist import Artist
from spotdl.types.playlist import Playlist
from spotdl.types.song import Song
from spotdl.utils.config import get_spotdl_path
from spotdl.utils.ffmpeg import FFMPEG_FORMATS
from spotdl.utils.search import (
    create_ytm_album,
    create_ytm_playlist,
    get_simple_songs,
    reinit_song,
)
from spotdl.utils.spotify import SpotifyClient
from spotdl.utils.web import (
    Client,
    app_state,
    get_ytm_url_type,
    is_ytm_url,
    validate_search_term,
)
from spotdl.web.utils import Signals, handle_signals

__all__ = ["router"]

router = APIRouter()

_TEMPLATE_DIR = str(Path(__file__).resolve().parent / "components")
templates = Jinja2Templates(directory=_TEMPLATE_DIR)
templates.env.autoescape = True  # Prevent XSS in all .j2 templates

# Max SSE iterations before auto-closing (safety valve against leaked connections)
_MAX_SSE_POLL_ITERATIONS = 7200  # ~6 hours at 3s intervals

_SEARCH_CACHE_TTL = 300
_search_cache: dict = {}
_inflight_searches: Dict[str, asyncio.Task] = {}
_inflight_downloads: Dict[str, asyncio.Task] = {}


def _cached_search(term: str):
    now = time.monotonic()
    cached = _search_cache.get(term)
    if cached and (now - cached[0]) < _SEARCH_CACHE_TTL:
        return cached[1]
    result = quick_search(term)
    _search_cache[term] = (now, result)
    if len(_search_cache) > 50:
        oldest = min(_search_cache, key=lambda k: _search_cache[k][0])
        del _search_cache[oldest]
    return result


async def _do_search(term: str):
    try:
        return await asyncio.to_thread(_cached_search, term)
    finally:
        _inflight_searches.pop(term, None)


async def _deduped_search(term: str):
    existing = _inflight_searches.get(term)
    if existing and not existing.done():
        return await asyncio.shield(existing)
    task = asyncio.create_task(_do_search(term))
    _inflight_searches[term] = task
    return await asyncio.shield(task)


def _safe_html_id(url: str) -> str:
    """Create a safe HTML element ID from a URL to prevent XSS via element IDs."""
    return "dl-" + hashlib.md5(url.encode()).hexdigest()[:12]


# Characters not allowed in Windows/Unix folder names
_INVALID_PATH_CHARS = frozenset('\\/:*?"<>|')


def _sanitize_folder_name(name: str) -> str:
    """Sanitize a string for use as a folder name."""
    cleaned = ''.join(c for c in name if c not in _INVALID_PATH_CHARS)
    return cleaned.strip('. ') or "Unknown"


def _output_with_subfolder(output_pattern: str, folder_name: str) -> str:
    """
    Insert a subfolder into the output path/pattern for playlist/album organization.

    Examples:
        "Downloaded/{artists} - {title}.{output-ext}"
        → "Downloaded/My Playlist/{artists} - {title}.{output-ext}"

        "/path/to/sessions/abc123"
        → "/path/to/sessions/abc123/My Playlist"
    """
    safe_name = _sanitize_folder_name(folder_name)
    brace_idx = output_pattern.find("{")
    if brace_idx > 0:
        # Pattern has placeholders: split dir from filename pattern
        base_dir = output_pattern[:brace_idx].rstrip("/\\")
        pattern = output_pattern[brace_idx:]
        return f"{base_dir}/{safe_name}/{pattern}"
    # Plain directory path: just append subfolder
    return output_pattern.rstrip("/\\") + "/" + safe_name


def _log_task_exception(task: asyncio.Task):
    """Callback to log unhandled exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        app_state.logger.error(f"Background task failed: {exc}")


# Register safe_html_id as a Jinja2 template global so templates can generate
# the same IDs that gen_download() uses for SSE element patching
templates.env.globals["safe_id"] = _safe_html_id


# URL downloads started from the search page, keyed by "client_id:url".
# Lets a re-entered URL attach to the already-running task instead of
# spawning a duplicate download.
_url_download_tasks: dict = {}


def _svg(inner: str, size: int = 16) -> str:
    """Small inline SVG icon for SSE-patched HTML (stroke style matches the
    icons.html.j2 macro used in templates)."""
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{inner}</svg>'
    )


_ICON_CHECK = _svg('<path d="m5 13 4 4 10-10"/>', 12)
_ICON_CHECK_MD = _svg('<path d="m5 13 4 4 10-10"/>', 15)
_ICON_ALERT = _svg('<path d="M12 8v5"/><path d="M12 16.5h.01"/><circle cx="12" cy="12" r="9"/>')
_ICON_ALERT_LG = _svg(
    '<path d="M12 8v5"/><path d="M12 16.5h.01"/><circle cx="12" cy="12" r="9"/>', 36
)
_ICON_SEARCH_LG = _svg('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.4-3.4"/>', 36)
_ICON_LIST = _svg('<path d="M4 6h.01M4 12h.01M4 18h.01M9 6h11M9 12h11M9 18h11"/>', 20)


def _notice_card(
    icon_svg: str,
    title: str,
    body: str,
    color: str = "var(--danger)",
    action_html: str = "",
) -> str:
    """Render a centered notice card into #search-list (errors, empty states)."""
    return (
        '<div id="search-list" class="max-w-xl mx-auto px-4 py-16">'
        '<div class="panel p-8 text-center fade-in">'
        f'<span class="inline-flex" style="color:{color};">{icon_svg}</span>'
        f'<p class="text-base font-semibold mt-4">{title}</p>'
        f'<p class="text-sm text-faint mt-1">{body}</p>'
        f"{action_html}"
        "</div></div>"
    )


def _spinner_card(message: str, extra_html: str = "") -> str:
    """Render a centered loading state into #search-list."""
    return (
        '<div id="search-list" class="max-w-xl mx-auto px-4 py-24">'
        '<div class="text-center fade-in">'
        '<span class="loading loading-spinner loading-md" style="color:var(--accent);"></span>'
        f'<p class="mt-4 text-sm text-muted">{message}</p>'
        f"{extra_html}"
        "</div></div>"
    )


def _url_download_card(
    *,
    title: str,
    subtitle: str,
    cover_url: Optional[str] = None,
    state: str = "downloading",
    pct: int = 0,
    done: int = 0,
    failed: int = 0,
    total: int = 1,
) -> str:
    """
    Render the inline download status card shown on the search page after
    the user enters a Spotify link. States: downloading | done | partial | failed.
    """
    safe_title = _esc(title)
    safe_subtitle = _esc(subtitle)

    if cover_url:
        media = (
            f'<img src="{_esc(cover_url)}" '
            f'class="w-12 h-12 rounded-lg object-cover shrink-0" loading="lazy" />'
        )
    else:
        media = (
            '<div class="w-12 h-12 rounded-lg flex items-center justify-center shrink-0" '
            f'style="background:var(--surface-2);color:var(--accent);">{_ICON_LIST}</div>'
        )

    downloads_link = (
        '<a href="/downloads" class="text-xs hover:underline" '
        'style="color:var(--accent);opacity:0.75;">Open Downloads &rarr;</a>'
    )

    if state == "downloading":
        counts = ""
        if total > 1:
            counts = (
                f'<span class="text-xs text-faint">{done} of {total}'
                + (
                    f' &middot; <span style="color:var(--danger);">{failed} failed</span>'
                    if failed
                    else ""
                )
                + "</span>"
            )
        status = (
            '<div class="flex items-center justify-between mb-2">'
            '<div class="flex items-center gap-2">'
            '<span class="loading loading-spinner loading-xs" style="color:var(--accent);"></span>'
            '<span class="text-sm font-medium" style="color:var(--accent);">Downloading</span>'
            f"</div>{counts}</div>"
            '<div class="flex items-center gap-3">'
            f'<progress class="progress-min flex-1" value="{pct}" max="100"></progress>'
            f'<span class="text-sm font-mono font-semibold w-11 text-right" '
            f'style="color:var(--accent);">{pct}%</span></div>'
        )
        footer = (
            '<a href="/downloads" class="text-xs hover:underline" '
            'style="color:var(--accent);opacity:0.75;">View details in Downloads &rarr;</a>'
        )
    elif state == "done":
        msg = "Downloaded" if total == 1 else f"Downloaded all {total} songs"
        status = (
            '<div class="flex items-center gap-2">'
            '<span style="display:inline-flex;width:20px;height:20px;border-radius:50%;'
            'background:var(--accent);color:var(--accent-ink);align-items:center;'
            f'justify-content:center;">{_ICON_CHECK}</span>'
            f'<span class="text-sm font-semibold" style="color:var(--accent);">{msg}</span></div>'
            '<p class="text-xs text-faint mt-1.5">Saved to your downloads folder</p>'
        )
        footer = downloads_link
    elif state == "partial":
        status = (
            '<div class="flex items-center gap-2" style="color:var(--warn);">'
            f"{_ICON_ALERT}"
            f'<span class="text-sm font-semibold">{done} downloaded &middot; {failed} failed</span></div>'
            '<p class="text-xs text-faint mt-1.5">Some tracks had no match or the provider blocked them</p>'
        )
        footer = downloads_link
    else:  # failed
        status = (
            '<div class="flex items-center gap-2" style="color:var(--danger);">'
            f"{_ICON_ALERT}"
            '<span class="text-sm font-semibold">Download failed</span></div>'
            '<p class="text-xs text-faint mt-1.5">No match found, or the audio provider blocked the download. '
            "Try again or switch providers in Settings.</p>"
        )
        footer = ""

    footer_html = (
        f'<div class="mt-4 pt-3" style="border-top:1px solid var(--line);">{footer}</div>'
        if footer
        else ""
    )

    return (
        '<div id="search-list" class="max-w-xl mx-auto px-4 py-10">'
        '<div class="panel p-5 fade-in">'
        f'<div class="flex items-center gap-3.5 mb-4">{media}'
        '<div class="flex-1 min-w-0">'
        f'<p class="text-sm font-semibold truncate">{safe_title}</p>'
        f'<p class="text-xs text-faint truncate mt-0.5">{safe_subtitle}</p></div></div>'
        f"{status}{footer_html}</div></div>"
    )


def _song_from_track_item(item: dict) -> Optional[Song]:
    """
    Build a lightweight Song from an official-API-shaped track dict
    (what spotipy returns, and what SpotipyFree's formatter produces).
    """
    try:
        artists = [
            a.get("name", "") for a in item.get("artists", []) if a.get("name")
        ] or ["Unknown"]
        album = item.get("album")
        album = album if isinstance(album, dict) else {}
        images = album.get("images") or []
        cover_url = images[-1].get("url") if images else None
        return Song.from_missing_data(
            name=item.get("name") or "Unknown",
            artists=artists,
            artist=artists[0],
            album_name=album.get("name") or "",
            duration=int((item.get("duration_ms") or 0) / 1000),
            song_id=item.get("id") or "",
            explicit=bool(item.get("explicit", False)),
            url=item.get("external_urls", {}).get("spotify", ""),
            cover_url=cover_url,
        )
    except Exception:  # pylint: disable=broad-except
        return None  # skip malformed entries rather than failing the search


def _album_card_type(raw_type: str) -> str:
    """ALBUM -> Album, EP -> EP, SINGLE -> Single."""
    raw_type = (raw_type or "ALBUM").strip()
    return "EP" if raw_type.upper() == "EP" else raw_type.capitalize()


def quick_search(search_term: str) -> tuple:
    """
    Fast text search for the web UI. Returns (songs, albums, artists) from a
    single API call. The stock get_search_results() re-fetches every result
    with Song.from_url (3 API calls per track, ~100 tracks per search), which
    is far too slow for an interactive UI — and it never returns albums or
    artists at all. The download buttons re-fetch the chosen item only.
    """
    if SpotifyClient.is_using_official_api():
        raw = SpotifyClient().search(search_term, type="track,album,artist")
        songs = [
            song
            for item in (raw or {}).get("tracks", {}).get("items", [])[:40]
            if (song := _song_from_track_item(item)) is not None
        ]
        albums = []
        for item in (raw or {}).get("albums", {}).get("items", [])[:6]:
            artist_names = [
                a.get("name", "") for a in item.get("artists", []) if a.get("name")
            ]
            albums.append(
                {
                    "name": item.get("name") or "Unknown",
                    "artist": ", ".join(artist_names[:2]) or "Unknown",
                    "year": (item.get("release_date") or "")[:4],
                    "album_type": _album_card_type(item.get("album_type", "")),
                    "cover_url": (item.get("images") or [{}])[0].get("url"),
                    "url": item.get("external_urls", {}).get("spotify", ""),
                }
            )
        artists = []
        for item in (raw or {}).get("artists", {}).get("items", [])[:4]:
            artists.append(
                {
                    "name": item.get("name") or "Unknown",
                    "verified": False,
                    "cover_url": (item.get("images") or [{}])[0].get("url"),
                    "url": item.get("external_urls", {}).get("spotify", ""),
                }
            )
        return songs, albums, artists

    # Free client: one raw searchDesktop call already contains every result
    # section (tracksV2, albumsV2, ...). SpotipyFree's search() throws away
    # everything except tracks, so query the raw endpoint directly.
    from spotapi.public import client_pool  # pylint: disable=import-outside-toplevel
    from spotapi.song import Song as _SpotapiSong  # pylint: disable=import-outside-toplevel
    from SpotipyFree.Formatter import SpotifyFormatter  # pylint: disable=import-outside-toplevel

    pooled_client = client_pool.get()
    try:
        raw = _SpotapiSong(client=pooled_client).query_songs(search_term, limit=40)
    finally:
        client_pool.put(pooled_client)
    search_v2 = (raw.get("data") or {}).get("searchV2") or {}

    songs = []
    for result in (search_v2.get("tracksV2") or {}).get("items", [])[:40]:
        data = (result.get("item") or {}).get("data") or {}
        if data.get("__typename") != "Track":
            continue
        try:
            formatted = SpotifyFormatter.formatTrack(
                data, SpotifyFormatter.formatArtists(data["artists"]["items"])
            )
        except Exception:  # pylint: disable=broad-except
            continue
        song = _song_from_track_item(formatted)
        if song is not None:
            songs.append(song)

    albums = []
    for wrapper in (search_v2.get("albumsV2") or {}).get("items", [])[:6]:
        data = wrapper.get("data") or {}
        if data.get("__typename") != "Album":
            continue
        artist_items = (data.get("artists") or {}).get("items") or []
        artist_names = [
            (a.get("profile") or {}).get("name", "") for a in artist_items
        ]
        artist_names = [name for name in artist_names if name]
        sources = (data.get("coverArt") or {}).get("sources") or []
        uri = data.get("uri") or ""
        album_id = uri.rsplit(":", 1)[-1] if ":" in uri else ""
        if not album_id:
            continue
        albums.append(
            {
                "name": data.get("name") or "Unknown",
                "artist": ", ".join(artist_names[:2]) or "Unknown",
                "year": (data.get("date") or {}).get("year") or "",
                "album_type": _album_card_type(data.get("type", "")),
                "cover_url": sources[0].get("url") if sources else None,
                "url": f"https://open.spotify.com/album/{album_id}",
            }
        )

    artists = []
    for wrapper in (search_v2.get("artists") or {}).get("items", [])[:4]:
        data = wrapper.get("data") or {}
        if data.get("__typename") != "Artist":
            continue
        uri = data.get("uri") or ""
        artist_id = uri.rsplit(":", 1)[-1] if ":" in uri else ""
        if not artist_id:
            continue
        sources = (
            ((data.get("visuals") or {}).get("avatarImage") or {}).get("sources")
        ) or []
        verified = (
            ((data.get("onPlatformReputationTrait") or {}).get("verification") or {})
            .get("isVerified", False)
        )
        artists.append(
            {
                "name": (data.get("profile") or {}).get("name") or "Unknown",
                "verified": bool(verified),
                "cover_url": sources[0].get("url") if sources else None,
                "url": f"https://open.spotify.com/artist/{artist_id}",
            }
        )
    return songs, albums, artists


def _batch_progress(tracker: dict, urls: list) -> tuple:
    """
    Compute (overall_pct, done_count, failed_count) for a batch of song URLs
    from the progress tracker. Songs not yet in the tracker count as 0%.
    """
    progress_sum = 0
    done = 0
    failed = 0
    for url in urls:
        entry = tracker.get(url)
        if entry is None:
            continue
        if entry.message == "Error":
            failed += 1
            continue
        pct = min(int(entry.progress or 0), 100)
        progress_sum += pct
        if pct >= 100:
            done += 1
    overall = int(progress_sum / len(urls)) if urls else 0
    return overall, done, failed


# PATHS


@router.get("/")
async def home(request: Request):
    """
    Handle the home page request.
    """
    return templates.TemplateResponse(
        name="home.html.j2",
        context={"request": request, "__version__": __version__},
    )


@router.get("/search")
async def search(q: Optional[str], request: Request):
    """
    Handle the search input.
    """
    return templates.TemplateResponse(
        name="search.html.j2",
        context={"request": request, "__version__": __version__, "search_term": q},
    )


@router.get("/downloads")
async def downloads(request: Request):
    """
    Handle the downloads page request.
    """
    return templates.TemplateResponse(
        name="downloads.html.j2",
        context={
            "request": request,
            "__version__": __version__,
        },
    )


# ACTIONS


@router.get("/client/load")
@datastar_response
async def handle_get_client_load(datastar_signals: ReadSignals):
    """
    Handle the loading of the client.
    """
    app_state.logger.info("Loading client...")
    signals = handle_signals(datastar_signals)
    if not signals.client_id:
        # Generate a new client ID if not provided
        app_state.logger.warning("No client ID provided, generating a new one.")
        signals.client_id = uuid.uuid4().hex

    found_client = Client.get_instance(signals.client_id)
    if found_client is None:
        # Create the client off the event loop (Downloader init is slow);
        # it registers itself in app_state.clients immediately
        app_state.logger.info(
            f"Creating new client with ID {signals.client_id}..."
        )
        client = await asyncio.get_running_loop().run_in_executor(
            None, Client, signals.client_id
        )
    else:
        client = found_client
    await client.connect()

    # Send the connected status and the client ID to the browser
    yield SSE.patch_elements(
        '<div id="status" class="icon-btn" title="Connected">'
        '<span class="dot dot-on"></span></div>'
    )
    yield SSE.patch_signals(
        {
            "client_id": client.client_id,
        }
    )
    try:
        # Wait for client to fully initialize (Downloader, ProgressHandler, etc.)
        while not getattr(client, '_initialized', False):
            await asyncio.sleep(0.5)
        last_count = None
        for _ in range(_MAX_SSE_POLL_ITERATIONS):
            song_count = len(
                client.downloader.progress_handler.progress_tracker.songs
            )
            # Only send a patch when the badge actually changes
            if song_count != last_count:
                last_count = song_count
                yield SSE.patch_elements(
                    f"""<p id="overall-completed-tasks" class="text-xs">{song_count}</p>"""
                )
            await asyncio.sleep(3)
    finally:
        app_state.logger.info(f"[{signals.client_id}] Unloading client...")
        await client.disconnect()


@router.get("/client/search")
@datastar_response
async def handle_get_client_search(datastar_signals: ReadSignals):
    """
    Handle the search input.
    """
    app_state.logger.info("Loading search...")
    signals = handle_signals(datastar_signals)
    app_state.logger.info(f"[{signals.client_id}] Search term: {signals.search_term}")
    is_valid_url = validate_search_term(signals.search_term)

    if is_valid_url:
        # YouTube Music list/album link — use the rich playlist download flow
        if is_ytm_url(signals.search_term) and get_ytm_url_type(signals.search_term) in (
            "playlist",
            "album",
        ):
            app_state.logger.info(
                f"[{signals.client_id}] YTM list URL detected, starting playlist download..."
            )
            yield SSE.redirect("/downloads")
            async for update in gen_download_ytm_list(signals):
                yield update
            return

        # YouTube Music single track link — resolve via YTM + Spotify match
        if is_ytm_url(signals.search_term) and get_ytm_url_type(signals.search_term) == "track":
            app_state.logger.info(
                f"[{signals.client_id}] YTM track URL detected, resolving..."
            )
            yield SSE.redirect("/downloads")
            async for update in gen_download_ytm_track(signals):
                yield update
            return

        app_state.logger.info(
            f"[{signals.client_id}] Valid URL detected, resolving..."
        )

        # On a first-ever visit this request can fire before /client/load
        # has assigned a client_id. Patch a short-delay retry element so the
        # request re-sends itself once the client_id signal has arrived.
        if not signals.client_id:
            try:
                retries = int((datastar_signals or {}).get("search_retries", 0) or 0)
            except (TypeError, ValueError):
                retries = 0
            if retries >= 8:
                yield SSE.patch_elements(_notice_card(
                    _ICON_ALERT_LG,
                    "Not connected",
                    "Could not establish a session. Refresh the page and try again.",
                ))
                return
            yield SSE.patch_signals({"search_retries": retries + 1})
            yield SSE.patch_elements(_spinner_card(
                "Connecting...",
                '<span data-on-load__delay.800ms="@get(\'/client/search\')"></span>',
            ))
            return

        # Show resolving state immediately
        yield SSE.patch_elements(_spinner_card("Resolving link..."))

        # Resolve URL to songs
        try:
            songs, folder_name = await asyncio.get_running_loop().run_in_executor(
                None, songs_from_url, signals.search_term
            )
        except Exception as exc:
            app_state.logger.error(f"[{signals.client_id}] URL resolve failed: {exc}")
            yield SSE.patch_elements(_notice_card(
                _ICON_ALERT_LG,
                "Could not resolve this link",
                "The URL may be invalid or the content is unavailable.",
                action_html=(
                    '<div class="mt-6"><button class="btn-accent text-sm" '
                    'data-on-click="goto(\'/\')">Back to Home</button></div>'
                ),
            ))
            return

        if not songs:
            yield SSE.patch_elements(_notice_card(
                _ICON_SEARCH_LG,
                "No tracks found",
                "This link doesn't contain any downloadable songs.",
                color="var(--faint)",
            ))
            return

        # The client must exist to run and track the download
        client = await wait_for_client(signals.client_id)
        if client is None:
            yield SSE.patch_elements(_notice_card(
                _ICON_ALERT_LG,
                "Session expired",
                "Refresh the page and try again.",
            ))
            return

        # Start the background download, or attach to the one already
        # running if the same URL was submitted again
        signals.song_url = signals.search_term
        download_key = f"{signals.client_id}:{signals.song_url}"
        for stale_key in [k for k, t in _url_download_tasks.items() if t.done()]:
            _url_download_tasks.pop(stale_key, None)
        task = _url_download_tasks.get(download_key)
        if task is None:
            task = asyncio.create_task(
                background_download(signals, songs=songs, folder_name=folder_name)
            )
            task.add_done_callback(_log_task_exception)
            _url_download_tasks[download_key] = task

        total = len(songs)
        urls = [song.url for song in songs]
        if total == 1 and folder_name is None:
            title = songs[0].name
            subtitle = songs[0].artist
            cover = songs[0].cover_url
        else:
            title = folder_name or "Spotify collection"
            subtitle = f"{total} song" if total == 1 else f"{total} songs"
            cover = None

        # Stream live percentage updates until the batch finishes
        yield SSE.patch_elements(
            _url_download_card(
                title=title, subtitle=subtitle, cover_url=cover,
                state="downloading", pct=0, total=total,
            )
        )
        last_render = None
        grace_polls = 0
        for _ in range(_MAX_SSE_POLL_ITERATIONS):
            # Re-read each iteration: the downloader can be re-created
            # when the user saves settings mid-download
            tracker = client.downloader.progress_handler.progress_tracker.songs
            pct, done_count, failed_count = _batch_progress(tracker, urls)
            render = (pct, done_count, failed_count)
            if render != last_render:
                last_render = render
                yield SSE.patch_elements(
                    _url_download_card(
                        title=title, subtitle=subtitle, cover_url=cover,
                        state="downloading", pct=pct,
                        done=done_count, failed=failed_count, total=total,
                    )
                )
            if task.done():
                # Give the tracker a few extra polls to settle, then stop
                if done_count + failed_count >= total or grace_polls >= 6:
                    break
                grace_polls += 1
            await asyncio.sleep(0.8)

        # Final state card
        tracker = client.downloader.progress_handler.progress_tracker.songs
        _, done_count, _ = _batch_progress(tracker, urls)
        if done_count >= total:
            final_state = "done"
        elif done_count > 0:
            final_state = "partial"
        else:
            final_state = "failed"
        app_state.logger.info(
            f"[{signals.client_id}] URL download finished: "
            f"{done_count}/{total} ok ({final_state})"
        )
        yield SSE.patch_elements(
            _url_download_card(
                title=title, subtitle=subtitle, cover_url=cover,
                state=final_state, pct=100,
                done=done_count, failed=total - done_count, total=total,
            )
        )
        return

    # Regular text search (non-URL); cached and de-duplicated per term
    try:
        songs, albums, artists = await _deduped_search(signals.search_term)
    except Exception as exc:
        app_state.logger.error(f"[{signals.client_id}] Search failed: {exc}")
        yield SSE.patch_elements(_notice_card(
            _ICON_ALERT_LG,
            "Search failed",
            "Something went wrong. Please try again.",
            color="var(--faint)",
        ))
        return

    yield SSE.patch_elements(
        templates.get_template("search-list.html.j2").render(
            songs=songs,
            albums=albums,
            artists=artists,
        )
    )


@router.get("/client/downloads")
@datastar_response
async def handle_get_client_downloads(datastar_signals: ReadSignals):
    """
    Handle the retrieval of client downloads.
    """
    app_state.logger.info("Loading downloads...")
    signals = handle_signals(datastar_signals)
    app_state.logger.info(f"[{signals.client_id}] Downloads requested.")
    # Wait for /client/load (fired in parallel) to finish creating the client
    client = await wait_for_client(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"[{signals.client_id}] Client not found, cannot load downloads."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return
    last_snapshot = None
    idle_ticks = 0
    for _ in range(_MAX_SSE_POLL_ITERATIONS):
        client_song_downloads = (
            client.downloader.progress_handler.progress_tracker.songs
        )
        snapshot = tuple(
            (url, s.progress, s.message)
            for url, s in client_song_downloads.items()
        )
        if snapshot != last_snapshot:
            yield SSE.patch_elements(
                templates.get_template("download-list.html.j2").render(
                    client_song_downloads=client_song_downloads.values()
                )
            )
            last_snapshot = snapshot
            idle_ticks = 0
        else:
            idle_ticks += 1
        interval = 1 if idle_ticks < 5 else 2 if idle_ticks < 30 else 4
        await asyncio.sleep(interval)


@router.get("/client/settings")
@datastar_response
async def handle_get_client_settings(datastar_signals: ReadSignals):
    """
    Handle the retrieval of client settings.
    """
    signals = handle_signals(datastar_signals)
    client = Client.get_instance(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"Client {signals.client_id} not found, cannot update settings."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return
    app_state.logger.info(f"[{signals.client_id}] Sending client settings...")
    yield SSE.patch_signals(
        {
            "downloader_settings": cast(Any, client.downloader_settings),
        }
    )


@router.post("/client/settings")
@datastar_response
async def handle_post_client_settings(datastar_signals: ReadSignals):
    """
    Handle the update of client settings.
    """
    signals = handle_signals(datastar_signals)
    client = Client.get_instance(signals.client_id)
    if client is not None:
        app_state.logger.info(f"[{signals.client_id}] Updating settings...")
        if signals.downloader_settings is not None:
            client.downloader_settings = signals.downloader_settings
        yield SSE.patch_elements("""
                <div id="settings-status">
                    <div id="settings-is-saved" class="alert alert-success shadow-lg">
                        <div>
                            <svg 
                            xmlns="http://www.w3.org/2000/svg" 
                            class="stroke-current flex-shrink-0 h-6 w-6" 
                            fill="none"
                            viewBox="0 0 24 24">
                                <path 
                                stroke-linecap="round" 
                                stroke-linejoin="round" 
                                stroke-width="2"
                                d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                            </svg>
                            <span>Changes saved</span>
                        </div>
                    </div>
                </div>
            """)
        yield SSE.patch_signals(
            {
                "downloader_settings": cast(Any, client.downloader_settings),
            }
        )
    else:
        app_state.logger.warning(
            f"[{signals.client_id}] Client not found, cannot update settings."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        yield SSE.patch_elements("""
                <div id="settings-status">
                    <div id="settings-is-not-saved" class="alert alert-error shadow-lg">
                        <div>
                            <svg 
                            xmlns="http://www.w3.org/2000/svg" 
                            class="stroke-current 
                            flex-shrink-0 h-6 w-6" 
                            fill="none"
                            viewBox="0 0 24 24">
                                <path 
                                stroke-linecap="round" 
                                stroke-linejoin="round" 
                                stroke-width="2"
                d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z" 
                            />
                            </svg>
                            <span>Error! Unable to save settings</span>
                        </div>
                    </div>
                </div>
            """)
    #  sleep for 3 seconds then clear the status message
    await asyncio.sleep(3)
    yield SSE.patch_elements("""
            <div id="settings-status">
            </div>
        """)


@router.post("/client/download/")
@datastar_response
async def handle_post_client_download(datastar_signals: ReadSignals):
    """
    Handle the download request from the client.
    """
    signals = handle_signals(datastar_signals)
    async for update in gen_download(signals):
        yield update


# HELPERS


async def wait_for_client(
    client_id: str, timeout: float = 15.0, wait_init: bool = True
) -> Optional[Client]:
    """
    Wait for a client to become available and fully initialized, with a timeout.
    Handles the race condition where /client/load is still creating the client
    when other SSE endpoints fire.
    """
    # Poll the registry directly (get_instance error-logs every miss,
    # which floods the log while we legitimately wait for creation)
    client = app_state.clients.get(client_id) if client_id else None
    if client is not None:
        if wait_init:
            # Wait for full initialization (downloader, progress_handler, etc.)
            elapsed = 0.0
            while not getattr(client, '_initialized', False) and elapsed < timeout:
                await asyncio.sleep(0.3)
                elapsed += 0.3
        return client

    if not client_id:
        return None

    # Client is being created by /client/load in another request, wait briefly
    elapsed = 0.0
    interval = 0.5
    while elapsed < timeout:
        await asyncio.sleep(interval)
        elapsed += interval
        client = app_state.clients.get(client_id)
        if client is not None:
            if wait_init:
                while not getattr(client, '_initialized', False) and elapsed < timeout:
                    await asyncio.sleep(0.3)
                    elapsed += 0.3
            return client

    app_state.logger.warning("Client %s not found after %.0fs", client_id, timeout)
    return None


# Track URLs currently being downloaded to prevent duplicate requests
_active_downloads: set = set()


def songs_from_url(url: str):
    """
    Resolve any Spotify URL (track, album, playlist, artist) to a list of Songs.
    Uses fetch_songs=False so we rely on get_metadata() which already extracts
    full track info from the Spotify playlist/album response in a single API call,
    instead of making N additional Song.from_url() calls.

    Returns (songs, folder_name) tuple. folder_name is the playlist/album/artist
    name (used for subfolder organization), or None for individual tracks.
    """
    if "://open.spotify.com/playlist/" in url:
        playlist = Playlist.from_url(url, fetch_songs=False)
        app_state.logger.info(
            f"Playlist '{playlist.name}': {len(playlist.songs)} songs"
        )
        return playlist.songs, playlist.name
    if "://open.spotify.com/album/" in url:
        album = Album.from_url(url, fetch_songs=False)
        app_state.logger.info(
            f"Album '{album.name}': {len(album.songs)} songs"
        )
        return album.songs, album.name
    if "://open.spotify.com/artist/" in url:
        artist = Artist.from_url(url, fetch_songs=False)
        app_state.logger.info(
            f"Artist '{artist.name}': {len(artist.songs)} songs"
        )
        return artist.songs, artist.name
    return [Song.from_url(url)], None


async def background_download(
    signals: Signals,
    songs: Optional[list] = None,
    folder_name: Optional[str] = None,
):
    """
    Run a download in the background (fire-and-forget).
    Used when search detects a valid URL on the search page.
    The download runs independently of any SSE stream.
    Supports tracks, albums, playlists, and artists.
    Pass pre-resolved songs/folder_name to skip a duplicate URL resolve.
    """
    # Prevent duplicate downloads of the same URL
    download_key = f"{signals.client_id}:{signals.song_url}"
    if download_key in _active_downloads:
        app_state.logger.info(
            f"[{signals.client_id}] Download already in progress: {signals.song_url}"
        )
        return
    _active_downloads.add(download_key)

    client = None
    try:
        client = await wait_for_client(signals.client_id)
        if client is None:
            app_state.logger.warning(
                f"[{signals.client_id}] Client not found for background download."
            )
            return

        # Pin the client for the whole batch so the inactivity timer can't
        # remove it (and its progress tracker) if the user closes the page
        await client.connect()

        app_state.logger.info(
            f"[{signals.client_id}] Background download started: {signals.song_url}"
        )

        # Determine base output path
        if app_state.web_settings.get("web_use_output_dir", False):
            output = client.downloader_settings["output"]
        else:
            output = str(
                (get_spotdl_path() / f"web/sessions/{client.client_id}").absolute()
            )

        # Resolve URL to list of songs unless the caller already did
        if songs is None:
            songs, folder_name = await asyncio.get_running_loop().run_in_executor(
                None, songs_from_url, signals.song_url
            )

        # Add subfolder for playlists/albums/artists
        if folder_name:
            output = _output_with_subfolder(output, folder_name)
            app_state.logger.info(
                f"[{signals.client_id}] Saving to subfolder: {_sanitize_folder_name(folder_name)}"
            )

        app_state.logger.info(
            f"[{signals.client_id}] Resolved {len(songs)} song(s) from URL"
        )

        # Use the running event loop's executor for downloads instead of
        # client.downloader.pool_download() which uses a separate loop
        running_loop = asyncio.get_running_loop()

        completed = 0
        failed = 0
        for i, song in enumerate(songs, 1):
            try:
                app_state.logger.info(
                    f"[{signals.client_id}] Downloading ({i}/{len(songs)}): "
                    f"{song.artist} - {song.name}"
                )
                # Pass the output template per call instead of mutating the
                # shared downloader settings, so concurrent downloads
                # (another batch or a single song) can't clobber each other
                _, path = await running_loop.run_in_executor(
                    None, client.downloader.search_and_download, song, output
                )
                if path is None:
                    failed += 1
                    app_state.logger.error(
                        f"[{signals.client_id}] Failed ({i}/{len(songs)}): {song.name}"
                    )
                else:
                    completed += 1
                    app_state.logger.info(
                        f"[{signals.client_id}] Done ({i}/{len(songs)}): {path}"
                    )
            except Exception as song_err:
                failed += 1
                app_state.logger.error(
                    f"[{signals.client_id}] Error downloading {song.name}: {song_err}"
                )
        app_state.logger.info(
            f"[{signals.client_id}] Batch complete: "
            f"{completed} downloaded, {failed} failed, {len(songs)} total"
        )
    except Exception as exception:
        app_state.logger.error(
            f"[{signals.client_id}] Background download error: {exception}"
        )
    finally:
        _active_downloads.discard(download_key)
        if client is not None:
            await client.disconnect()


async def gen_download_ytm_track(signals: Signals):
    """
    Download a single YouTube Music track (music.youtube.com/watch?v=...).
    Uses get_simple_songs to do the YTM metadata fetch + Spotify match,
    then downloads and shows progress via the standard per-song download list.
    """
    client = Client.get_instance(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"[{signals.client_id}] Client not found, cannot download YTM track."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return

    url = signals.search_term

    if app_state.web_settings.get("web_use_output_dir", False):
        client.downloader.settings["output"] = client.downloader_settings["output"]
    else:
        client.downloader.settings["output"] = str(
            (get_spotdl_path() / f"web/sessions/{client.client_id}").absolute()
        )

    try:
        # get_simple_songs handles music.youtube.com/watch?v= by calling
        # ytmusicapi.get_song(), then searching Spotify for a metadata match
        songs = await asyncio.to_thread(get_simple_songs, [url])
        if not songs:
            raise ValueError(f"Could not resolve YTM track: {url}")
        song = songs[0]
        app_state.logger.info(
            f"[{signals.client_id}] YTM track resolved: {song.display_name}"
        )
        _, path = await client.downloader.pool_download(song)
        if path is None:
            app_state.logger.error(
                f"[{signals.client_id}] YTM track path=None: {song.display_name}"
            )
    except Exception as exc:
        app_state.logger.error(
            f"[{signals.client_id}] YTM track download error: {exc}"
        )


async def gen_download_ytm_list(signals: Signals):
    """
    Download every song in a YouTube Music playlist or album, emitting a
    live playlist-card element that updates as each track finishes.
    """
    client = Client.get_instance(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"[{signals.client_id}] Client not found, cannot start YTM download."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return

    url = signals.search_term
    url_type = get_ytm_url_type(url)
    card_id = f"ytm-list-{abs(hash(url)) % 10**8}"

    # ------------------------------------------------------------------
    # 1. Resolve playlist / album metadata (non-blocking)
    # ------------------------------------------------------------------
    try:
        if url_type == "album":
            song_list = await asyncio.to_thread(create_ytm_album, url, False)
        else:
            song_list = await asyncio.to_thread(create_ytm_playlist, url, False)
    except Exception as exc:
        app_state.logger.error(f"[{signals.client_id}] YTM metadata error: {exc}")
        yield SSE.patch_elements(
            templates.get_template("ytm-playlist-card.html.j2").render(
                card_id=card_id,
                list_name=url,
                list_type=url_type,
                total=0,
                done=0,
                failed=0,
                state="error",
                error_msg=str(exc),
            ),
            selector="#ytm-downloads",
            mode="append",
        )
        return

    total = len(song_list.songs)
    list_name = song_list.name
    app_state.logger.info(
        f"[{signals.client_id}] YTM {url_type} '{list_name}': {total} tracks"
    )

    # ------------------------------------------------------------------
    # 2. Append initial card into #ytm-downloads (downloading state)
    # ------------------------------------------------------------------
    yield SSE.patch_elements(
        templates.get_template("ytm-playlist-card.html.j2").render(
            card_id=card_id,
            list_name=list_name,
            list_type=url_type,
            total=total,
            done=0,
            failed=0,
            state="downloading",
            error_msg="",
        ),
        selector="#ytm-downloads",
        mode="append",
    )

    # ------------------------------------------------------------------
    # 3. Configure output folder  <session>/<Playlist Name>/
    # ------------------------------------------------------------------
    safe_name = "".join(
        c if c.isalnum() or c in " -_()" else "_" for c in list_name
    ).strip()

    if app_state.web_settings.get("web_use_output_dir", False):
        base_out = client.downloader_settings["output"]
        # Insert the playlist subfolder just before the first template token
        if "{" in base_out:
            prefix, rest = base_out.split("{", 1)
            session_out = prefix + safe_name + "/" + "{" + rest
        else:
            session_out = str(Path(base_out) / safe_name)
    else:
        session_out = str(
            (get_spotdl_path() / f"web/sessions/{client.client_id}/{safe_name}").absolute()
        )

    client.downloader.settings["output"] = session_out

    # ------------------------------------------------------------------
    # 4. Download tracks one-by-one, updating the card after each
    # ------------------------------------------------------------------
    done = 0
    failed = 0

    for song in song_list.songs:
        try:
            full_song = await asyncio.to_thread(reinit_song, song)
            _, path = await client.downloader.pool_download(full_song)
            if path is None:
                failed += 1
                app_state.logger.error(
                    f"[{signals.client_id}] YTM: path=None for '{song.name}'"
                )
            else:
                done += 1
        except Exception as exc:
            failed += 1
            app_state.logger.error(
                f"[{signals.client_id}] YTM download error '{song.name}': {exc}"
            )

        yield SSE.patch_elements(
            templates.get_template("ytm-playlist-card.html.j2").render(
                card_id=card_id,
                list_name=list_name,
                list_type=url_type,
                total=total,
                done=done,
                failed=failed,
                state="downloading",
                error_msg="",
            )
        )

    # ------------------------------------------------------------------
    # 5. Final card state
    # ------------------------------------------------------------------
    yield SSE.patch_elements(
        templates.get_template("ytm-playlist-card.html.j2").render(
            card_id=card_id,
            list_name=list_name,
            list_type=url_type,
            total=total,
            done=done,
            failed=failed,
            state="done",
            error_msg="",
        )
    )


async def gen_download(signals: Signals):
    """
    Generate the download process for the client.
    Streams real-time progress updates via SSE while downloading.
    """
    client = await wait_for_client(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"Client {signals.client_id} not found, cannot load downloads."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return

    # Ignore repeat clicks while this song is already downloading
    existing = _inflight_downloads.get(signals.song_url)
    if existing and not existing.done():
        app_state.logger.info(
            f"[{signals.client_id}] Download already in progress: {signals.song_url}"
        )
        return
    loop = asyncio.get_running_loop()
    inflight = loop.create_future()
    _inflight_downloads[signals.song_url] = inflight

    # Use a safe HTML ID derived from URL hash to prevent XSS
    btn_id = _safe_html_id(signals.song_url)
    try:
        app_state.logger.info(
            f"[{signals.client_id}] Download requested: {signals.song_url}"
        )
        yield SSE.patch_elements(
            f'<button id="{btn_id}" class="icon-btn soft shrink-0">'
            '<span class="loading loading-spinner loading-xs"></span></button>'
        )

        if app_state.web_settings.get("web_use_output_dir", False):
            output = client.downloader_settings["output"]
        else:
            output = str(
                (get_spotdl_path() / f"web/sessions/{client.client_id}").absolute()
            )

        # Fetch song metadata
        song = await loop.run_in_executor(None, Song.from_url, signals.song_url)
        app_state.logger.info(f"Downloading song: {song.artist} - {song.name}")

        # Start download as a concurrent future so we can poll progress.
        # The output template is passed per call (not via shared settings)
        # so this can't redirect a playlist/album batch downloading
        # concurrently — and vice versa.
        future = loop.run_in_executor(
            None, client.downloader.search_and_download, song, output
        )

        # Poll the progress tracker and stream updates while download runs
        last_pct = -1
        while not future.done():
            await asyncio.sleep(0.7)
            try:
                tracker = client.downloader.progress_handler.progress_tracker.songs
                for st in tracker.values():
                    if st.song.name == song.name:
                        pct = st.progress
                        if pct != last_pct and 0 < pct < 100:
                            last_pct = pct
                            yield SSE.patch_elements(
                                f'<button id="{btn_id}" class="icon-btn soft shrink-0">'
                                f'<span style="font-size:10px;font-weight:700;">{pct}%</span>'
                                '</button>'
                            )
                        break
            except (RuntimeError, AttributeError):
                pass  # Tracker may be modified concurrently

        # Download finished — get result
        _, path = future.result()

        if path is None:
            app_state.logger.error(f"Failure downloading {song.name}")
            yield SSE.patch_elements(
                f'<button id="{btn_id}" class="icon-btn danger-soft shrink-0" '
                f'title="Download failed">{_ICON_ALERT}</button>'
            )
        else:
            yield SSE.patch_elements(
                f'<button id="{btn_id}" class="icon-btn solid shrink-0" '
                f'title="Downloaded">{_ICON_CHECK_MD}</button>'
            )

    except Exception as exception:
        app_state.logger.error(f"Error downloading! {exception}")
        yield SSE.patch_elements(
            f'<button id="{btn_id}" class="icon-btn danger-soft shrink-0" '
            f'title="Download failed">{_ICON_ALERT}</button>'
        )
    finally:
        # Covers normal completion, errors, and the stream being closed
        if not inflight.done():
            inflight.set_result(None)
        _inflight_downloads.pop(signals.song_url, None)


# COMPONENTS


@router.get("/client/component/settings-content")
@datastar_response
async def handle_get_client_component_settings(datastar_signals: ReadSignals):
    """
    Handle the request for the client settings component.
    """
    signals = handle_signals(datastar_signals)
    client = Client.get_instance(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"Client {signals.client_id} not found, cannot update settings."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return
    app_state.logger.info(f"[{signals.client_id}] Loading settings view...")
    # clear state
    yield SSE.patch_elements("""<div id="component-settings-content"></div>""")
    # render the settings content
    yield SSE.patch_elements(
        templates.get_template("settings-content.html.j2").render(
            downloader_settings=client.downloader_settings,
            AUDIO_PROVIDERS=AUDIO_PROVIDERS,
            LYRICS_PROVIDERS=LYRICS_PROVIDERS,
            FORMATS=FFMPEG_FORMATS.keys(),
        )
    )
    yield SSE.patch_signals(
        {
            "downloader_settings": cast(Any, client.downloader_settings),
        }
    )


