"""
Module which contains the web client routes and functions.
"""

import asyncio
import uuid
from typing import Any, Optional, cast

from datastar_py.fastapi import ReadSignals
from datastar_py.fastapi import ServerSentEventGenerator as SSE
from datastar_py.fastapi import datastar_response
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from spotdl._version import __version__
from spotdl.download.downloader import AUDIO_PROVIDERS, LYRICS_PROVIDERS
from spotdl.types.song import Song
from spotdl.utils.config import get_spotdl_path
from spotdl.utils.ffmpeg import FFMPEG_FORMATS
from spotdl.utils.search import get_search_results
from spotdl.utils.web import Client, app_state, validate_search_term
from spotdl.web.utils import Signals, handle_signals

__all__ = ["router"]

router = APIRouter()

templates = Jinja2Templates(directory="spotdl/web/components")


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
        client = await asyncio.get_event_loop().run_in_executor(
            None, Client, signals.client_id
        )
    else:
        client = found_client
    await client.connect()

    # Send the client ID and connected status to the browser
    yield SSE.patch_elements("""<div id="status" class="btn btn-ghost btn-circle btn-sm opacity-80">
        <iconify-icon icon="clarity:check-circle-line" style="font-size: 20px; color: #1ed760;"></iconify-icon>
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
        while True:
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
    app_state.logger.info(f"[{signals.client_id}] Search term: {signals.search_term}")
    is_valid_url = validate_search_term(signals.search_term)

    if is_valid_url:
        # redirect client to downloads page and start download in background
        app_state.logger.info(
            f"[{signals.client_id}] Valid URL detected, redirecting to downloads..."
        )
        signals.song_url = signals.search_term
        # Fire-and-forget the download so it continues even after SSE closes
        asyncio.create_task(background_download(signals))
        yield SSE.redirect("/downloads")
        return

    songs = await asyncio.get_event_loop().run_in_executor(
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
    while True:
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


async def background_download(signals: Signals):
    """
    Run a download in the background (fire-and-forget).
    Used when search detects a valid URL and redirects to downloads page.
    The download runs independently of any SSE stream.
    """
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

    try:
        song = Song.from_url(signals.song_url)
        app_state.logger.info(f"[{signals.client_id}] Downloading: {song}")
        _, path = await client.downloader.pool_download(song)
        if path is None:
            app_state.logger.error(
                f"[{signals.client_id}] Failed to download {song.name}"
            )
        else:
            app_state.logger.info(
                f"[{signals.client_id}] Download complete: {path}"
            )
    except Exception as exception:
        app_state.logger.error(
            f"[{signals.client_id}] Background download error: {exception}"
        )


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
    yield SSE.patch_elements(f"""
            <button id="download-{signals.song_url}" class="btn btn-circle btn-sm border-0"
                style="background: rgba(30,215,96,0.2);">
                <span class="loading loading-spinner loading-xs" style="color: #1ed760;"></span>
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
        song = Song.from_url(signals.song_url)
        app_state.logger.info(f"Downloading song: {song}")

        # Download Song
        _, path = await client.downloader.pool_download(song)
        yield SSE.patch_elements(f"""
            <button id="download-{signals.song_url}" class="btn btn-circle btn-sm border-0 text-black"
                style="background: #1ed760;">
                <iconify-icon icon="clarity:check-line" style="font-size: 16px"></iconify-icon>
            </button>
        """)

        if path is None:
            app_state.logger.error(f"Failure downloading {song.name}")

        # return str(path.absolute())

    except Exception as exception:
        app_state.logger.error(f"Error downloading! {exception}")


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
    while True:
        t = templates.get_template("search-input-rotating-placeholder.html.j2").render(
            placeholder_item=placeholder_items[index]
        )
        yield SSE.patch_elements(t)
        await asyncio.sleep(5)
        index += 1
        if index >= len(placeholder_items):
            index = 0
