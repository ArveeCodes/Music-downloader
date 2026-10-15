"""
Module which contains the web client routes and functions.
"""

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, cast

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
from spotdl.utils.search import (
    create_ytm_album,
    create_ytm_playlist,
    get_search_results,
    reinit_song,
)
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

_SEARCH_CACHE_TTL = 300
_search_cache: dict = {}
_inflight_searches: Dict[str, asyncio.Task] = {}
_inflight_downloads: Dict[str, asyncio.Task] = {}


def _cached_search(term: str):
    now = time.monotonic()
    cached = _search_cache.get(term)
    if cached and (now - cached[0]) < _SEARCH_CACHE_TTL:
        return cached[1]
    result = get_search_results(term)
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
        client = Client(signals.client_id)
    else:
        found_client = Client.get_instance(signals.client_id)
        if found_client is None:
            # Create a new client if not found
            app_state.logger.warning(
                f"Client {signals.client_id} not found, creating new client..."
            )
            signals.client_id = uuid.uuid4().hex
            client = Client(signals.client_id)
        else:
            client = found_client
    await client.connect()

    # First send the client ID and then the home template.
    yield SSE.patch_elements("""<div id="status"></div>""")
    # Send the client ID to the client
    yield SSE.patch_signals(
        {
            "client_id": client.client_id,
        }
    )
    last_count = -1
    try:
        while True:
            count = len(client.downloader.progress_handler.progress_tracker.songs)
            if count != last_count:
                yield SSE.patch_elements(f"""<div id="overall-completed-tasks">
                    {count}
                    </div>""")
                last_count = count
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

        app_state.logger.info(
            f"[{signals.client_id}] Valid URL detected, redirecting to downloads..."
        )
        yield SSE.redirect("/downloads")
        signals.song_url = signals.search_term
        async for update in gen_download(signals):
            yield update
        return

    songs = await _deduped_search(signals.search_term)
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
    client = Client.get_instance(signals.client_id)
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
    while True:
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


async def _do_download(signals: Signals):
    """
    Execute the actual download. Returns the button HTML to patch.
    """
    client = Client.get_instance(signals.client_id)
    if client is None:
        return None

    if app_state.web_settings.get("web_use_output_dir", False):
        client.downloader.settings["output"] = client.downloader_settings["output"]
    else:
        client.downloader.settings["output"] = str(
            (get_spotdl_path() / f"web/sessions/{client.client_id}").absolute()
        )

    try:
        song = await asyncio.to_thread(Song.from_url, signals.song_url)
        app_state.logger.info(f"Downloading song: {song}")
        _, path = await client.downloader.pool_download(song)

        if path is None:
            app_state.logger.error(f"Failure downloading {song.name}")

        return "done"
    except Exception as exception:
        app_state.logger.error(f"Error downloading! {exception}")
        return "error"
    finally:
        _inflight_downloads.pop(signals.song_url, None)


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
    """
    client = Client.get_instance(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"Client {signals.client_id} not found, cannot load downloads."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return

    existing = _inflight_downloads.get(signals.song_url)
    if existing and not existing.done():
        app_state.logger.info(
            f"[{signals.client_id}] Download already in progress: {signals.song_url}"
        )
        return

    app_state.logger.info(
        f"[{signals.client_id}] Download requested: {signals.song_url}"
    )
    yield SSE.patch_elements(f"""
            <button id="download-{signals.song_url}" class="btn btn-primary btn-square loading">
                </button>
        """)

    task = asyncio.create_task(_do_download(signals))
    _inflight_downloads[signals.song_url] = task
    result = await asyncio.shield(task)

    if result == "done":
        yield SSE.patch_elements(f"""
            <button id="download-{signals.song_url}" class="btn btn-primary btn-square">
                    <iconify-icon icon="clarity:check-line" style="font-size: 24px"></iconify-icon>
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
    yield SSE.patch_signals(
        {
            "downloader_settings": cast(Any, client.downloader_settings),
        }
    )


