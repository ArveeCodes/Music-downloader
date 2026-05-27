"""
Module which contains the web client routes and functions.
"""

import asyncio
import hashlib
import uuid
from typing import Any, Optional, cast

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
from spotdl.utils.search import get_search_results
from spotdl.utils.web import Client, app_state, validate_search_term
from spotdl.web.utils import Signals, handle_signals

__all__ = ["router"]

router = APIRouter()

templates = Jinja2Templates(directory="spotdl/web/components")
templates.env.autoescape = True  # Prevent XSS in all .j2 templates

# Max SSE iterations before auto-closing (safety valve against leaked connections)
_MAX_SSE_POLL_ITERATIONS = 7200  # ~6 hours at 3s intervals


def _safe_html_id(url: str) -> str:
    """Create a safe HTML element ID from a URL to prevent XSS via element IDs."""
    return "dl-" + hashlib.md5(url.encode()).hexdigest()[:12]


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
        # Create a new client (registers itself in app_state.clients immediately)
        app_state.logger.info(
            f"Creating new client with ID {signals.client_id}..."
        )
        client = await asyncio.get_running_loop().run_in_executor(
            None, Client, signals.client_id
        )
    else:
        client = found_client
    await client.connect()

    # Send the client ID and connected status to the browser
    yield SSE.patch_elements("""<div id="status" class="btn btn-ghost btn-circle btn-sm opacity-80">
        <iconify-icon icon="clarity:check-circle-line" style="font-size: 18px; color: #00d4ff;"></iconify-icon>
    </div>""")
    yield SSE.patch_signals(
        {
            "client_id": client.client_id,
        }
    )
    try:
        # Wait for client to fully initialize (Downloader, ProgressHandler, etc.)
        while not getattr(client, '_initialized', False):
            await asyncio.sleep(0.5)
        for _ in range(_MAX_SSE_POLL_ITERATIONS):
            song_count = len(
                client.downloader.progress_handler.progress_tracker.songs
            )
            yield SSE.patch_elements(
                f"""<div id="overall-completed-tasks">{song_count}</div>"""
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
    app_state.logger.info(f"[{signals.client_id}] Search term: '{signals.search_term}'")
    is_valid_url = validate_search_term(signals.search_term)

    if is_valid_url:
        # redirect client to downloads page and start download in background
        app_state.logger.info(
            f"[{signals.client_id}] Valid URL detected, redirecting to downloads..."
        )
        signals.song_url = signals.search_term
        # Fire-and-forget the download so it continues even after SSE closes
        task = asyncio.create_task(background_download(signals))
        task.add_done_callback(_log_task_exception)
        yield SSE.redirect("/downloads")
        return

    songs = await asyncio.get_running_loop().run_in_executor(
        None, get_search_results, signals.search_term
    )
    yield SSE.patch_elements(
        templates.get_template("search-list.html.j2").render(
            songs=songs,
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
    client = await wait_for_client(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"[{signals.client_id}] Client not found, cannot load downloads."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return
    for _ in range(_MAX_SSE_POLL_ITERATIONS):
        client_song_downloads = (
            client.downloader.progress_handler.progress_tracker.songs
        )
        yield SSE.patch_elements(
            templates.get_template("download-list.html.j2").render(
                client_song_downloads=client_song_downloads.values()
            )
        )
        await asyncio.sleep(2)


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
    client = Client.get_instance(client_id)
    if client is not None:
        if wait_init:
            # Wait for full initialization (downloader, progress_handler, etc.)
            elapsed = 0.0
            while not getattr(client, '_initialized', False) and elapsed < timeout:
                await asyncio.sleep(0.3)
                elapsed += 0.3
        return client

    # Client is being created by /client/load in another request, wait briefly
    elapsed = 0.0
    interval = 0.5
    while elapsed < timeout:
        await asyncio.sleep(interval)
        elapsed += interval
        client = Client.get_instance(client_id)
        if client is not None:
            if wait_init:
                while not getattr(client, '_initialized', False) and elapsed < timeout:
                    await asyncio.sleep(0.3)
                    elapsed += 0.3
            return client

    return None


# Track URLs currently being downloaded to prevent duplicate requests
_active_downloads: set = set()


def songs_from_url(url: str):
    """
    Resolve any Spotify URL (track, album, playlist, artist) to a list of Songs.
    Uses fetch_songs=False so we rely on get_metadata() which already extracts
    full track info from the Spotify playlist/album response in a single API call,
    instead of making N additional Song.from_url() calls.

    URL type is determined by path segments (e.g. /playlist/, /album/) to avoid
    false positives from query parameters containing those words.
    """
    if "://open.spotify.com/playlist/" in url:
        playlist = Playlist.from_url(url, fetch_songs=False)
        app_state.logger.info(
            f"Playlist '{playlist.name}': {len(playlist.songs)} songs"
        )
        return playlist.songs
    if "://open.spotify.com/album/" in url:
        album = Album.from_url(url, fetch_songs=False)
        app_state.logger.info(
            f"Album '{album.name}': {len(album.songs)} songs"
        )
        return album.songs
    if "://open.spotify.com/artist/" in url:
        artist = Artist.from_url(url, fetch_songs=False)
        app_state.logger.info(
            f"Artist '{artist.name}': {len(artist.songs)} songs"
        )
        return artist.songs
    return [Song.from_url(url)]


async def background_download(signals: Signals):
    """
    Run a download in the background (fire-and-forget).
    Used when search detects a valid URL and redirects to downloads page.
    The download runs independently of any SSE stream.
    Supports tracks, albums, playlists, and artists.
    """
    # Prevent duplicate downloads of the same URL
    download_key = f"{signals.client_id}:{signals.song_url}"
    if download_key in _active_downloads:
        app_state.logger.info(
            f"[{signals.client_id}] Download already in progress: {signals.song_url}"
        )
        return
    _active_downloads.add(download_key)

    try:
        client = await wait_for_client(signals.client_id)
        if client is None:
            app_state.logger.warning(
                f"[{signals.client_id}] Client not found for background download."
            )
            return

        app_state.logger.info(
            f"[{signals.client_id}] Background download started: {signals.song_url}"
        )

        if app_state.web_settings.get("web_use_output_dir", False):
            client.downloader.settings["output"] = client.downloader_settings["output"]
        else:
            client.downloader.settings["output"] = str(
                (get_spotdl_path() / f"web/sessions/{client.client_id}").absolute()
            )

        # Resolve URL to list of songs (works for tracks, albums, playlists, artists)
        songs = await asyncio.get_running_loop().run_in_executor(
            None, songs_from_url, signals.song_url
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
                _, path = await running_loop.run_in_executor(
                    None, client.downloader.search_and_download, song
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


async def gen_download(signals: Signals):
    """
    Generate the download process for the client.
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
    app_state.logger.info(
        f"[{signals.client_id}] Download requested: {signals.song_url}"
    )
    # Use a safe HTML ID derived from URL hash to prevent XSS
    btn_id = _safe_html_id(signals.song_url)
    yield SSE.patch_elements(f"""
            <button id="{btn_id}" class="btn btn-circle btn-xs border-0"
                style="background: rgba(0,212,255,0.1);">
                <span class="loading loading-spinner loading-xs" style="color: #00d4ff;"></span>
            </button>
        """)

    if app_state.web_settings.get("web_use_output_dir", False):
        client.downloader.settings["output"] = client.downloader_settings["output"]
    else:
        client.downloader.settings["output"] = str(
            (get_spotdl_path() / f"web/sessions/{client.client_id}").absolute()
        )

    try:
        # Fetch song metadata
        song = await asyncio.get_running_loop().run_in_executor(
            None, Song.from_url, signals.song_url
        )
        app_state.logger.info(f"Downloading song: {song.artist} - {song.name}")

        # Download Song using running loop's executor to avoid cross-loop issues
        _, path = await asyncio.get_running_loop().run_in_executor(
            None, client.downloader.search_and_download, song
        )

        if path is None:
            app_state.logger.error(f"Failure downloading {song.name}")
            yield SSE.patch_elements(f"""
                <button id="{btn_id}" class="btn btn-circle btn-xs border-0"
                    style="background: rgba(239,68,68,0.12);">
                    <iconify-icon icon="clarity:error-standard-line" style="font-size: 14px; color: #f87171;"></iconify-icon>
                </button>
            """)
        else:
            yield SSE.patch_elements(f"""
                <button id="{btn_id}" class="btn btn-circle btn-xs border-0"
                    style="background: rgba(0,212,255,0.15);">
                    <iconify-icon icon="clarity:check-line" style="font-size: 14px; color: #00d4ff;"></iconify-icon>
                </button>
            """)

    except Exception as exception:
        app_state.logger.error(f"Error downloading! {exception}")
        yield SSE.patch_elements(f"""
            <button id="{btn_id}" class="btn btn-circle btn-xs border-0"
                style="background: rgba(239,68,68,0.12);">
                <iconify-icon icon="clarity:error-standard-line" style="font-size: 14px; color: #f87171;"></iconify-icon>
            </button>
        """)


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
    # spotify_client = SpotifyClient()
    # print(f"{spotify_client = }")
    yield SSE.patch_signals(
        {
            "downloader_settings": cast(Any, client.downloader_settings),
            # "spotify_settings": {
            #     "client_id": spotify_client.credential_manager.client_id
            # },
        }
    )


@router.get("/client/component/search-input-rotating-placeholder")
@datastar_response
async def handle_client_component_search_input_rotating_placeholder():
    """
    Handle the search input rotating placeholder component.
    """
    app_state.logger.info("Loading rotating-placeholder...")
    placeholder_items = [
        "All Eyes On Me - Bo Burnham",
        "https://open.spotify.com/track/4vfN00PlILRXy5dcXHQE9M?si=e4d9e7c044dd4a8f",
        "Lil Wayne",
        "Drive - Miley Cyrus",
        "Sofia - TMG",
        "Lightning Crashes - Live",
    ]
    index = 0
    for _ in range(_MAX_SSE_POLL_ITERATIONS):
        t = templates.get_template("search-input-rotating-placeholder.html.j2").render(
            placeholder_item=placeholder_items[index]
        )
        yield SSE.patch_elements(t)
        await asyncio.sleep(5)
        index = (index + 1) % len(placeholder_items)
