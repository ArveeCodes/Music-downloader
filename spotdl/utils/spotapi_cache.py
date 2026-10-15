"""
Patch spotapi's BaseClient so all instances share one negotiated session.

SpotipyFree creates a brand-new spotapi object (Song, Artist, PublicAlbum,
PublicPlaylist, Public, ...) for every API call. Each new object builds a
fresh BaseClient which re-negotiates the whole web-player session: fetching
open.spotify.com, solving the TOTP challenge, requesting a client token and
downloading every web-player JS chunk to extract GraphQL query hashes.
That costs ~20 seconds PER CALL (Song.from_url alone makes 3 calls = ~60s).

With this patch the negotiated state (tokens + hashes) is cached at module
level and reused by every BaseClient until it expires, so only the first
call pays the negotiation cost. A prewarm helper performs that first
negotiation in a background thread at startup.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

__all__ = ["apply_spotapi_cache", "prewarm_spotapi_session"]

logger = logging.getLogger(__name__)

# Negotiated BaseClient attributes worth sharing between instances
_STATE_FIELDS = (
    "js_pack",
    "client_version",
    "access_token",
    "client_token",
    "client_id",
    "device_id",
    "raw_hashes",
)

# Spotify web access tokens live ~1 hour; renegotiate well before that
_TTL_SECONDS = 30 * 60

_lock = threading.RLock()
_state: Dict[str, Any] = {}
_state_time: float = 0.0
_patched = False

# GraphQL persisted-query hashes, keyed by web-player client_version.
# These only change when Spotify ships a new web player, so they are
# persisted to disk — a restarted server can answer every API call without
# re-downloading the multi-megabyte JS bundles (the slow part of startup).
_hash_map: Dict[str, Dict[str, str]] = {}
_hash_map_loaded = False


def _is_fresh() -> bool:
    return bool(_state) and (time.time() - _state_time) < _TTL_SECONDS


def _hash_store_path() -> Optional[Path]:
    try:
        from spotdl.utils.config import get_spotdl_path

        return Path(get_spotdl_path()) / "spotapi-hashes.json"
    except Exception:  # pylint: disable=broad-except
        return None


def _load_hash_map() -> None:
    global _hash_map, _hash_map_loaded
    if _hash_map_loaded:
        return
    _hash_map_loaded = True
    path = _hash_store_path()
    if path is None or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _hash_map = data
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("Could not load spotapi hash store: %s", exc)


def _save_hash_map() -> None:
    path = _hash_store_path()
    if path is None:
        return
    try:
        path.write_text(json.dumps(_hash_map), encoding="utf-8")
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("Could not save spotapi hash store: %s", exc)


def _known_hashes_for(client_version: Any) -> Dict[str, str]:
    _load_hash_map()
    return _hash_map.get(str(client_version), {})


def apply_spotapi_cache() -> bool:
    """
    Install the shared-session patch on spotapi.BaseClient.
    Returns True if the patch is active, False if spotapi is unavailable.
    Safe to call multiple times.
    """
    global _patched
    if _patched:
        return True

    try:
        from spotapi.client import BaseClient
        from spotapi.types.alias import _Undefined
    except ImportError as exc:
        logger.debug("spotapi not available, cache patch skipped: %s", exc)
        return False

    def _restore(base: Any) -> None:
        for field, value in _state.items():
            if getattr(base, field, _Undefined) is _Undefined:
                setattr(base, field, value)

    def _save(base: Any) -> None:
        global _state_time
        for field in _STATE_FIELDS:
            value = getattr(base, field, _Undefined)
            if value is not _Undefined:
                _state[field] = value
        _state_time = time.time()

    def _wrap(method_name: str, satisfied: Callable[[Any], bool]) -> None:
        original = getattr(BaseClient, method_name)

        def wrapper(self: Any) -> None:
            with _lock:
                if _is_fresh():
                    _restore(self)
                    if satisfied(self):
                        return
                original(self)
                _save(self)

        wrapper.__name__ = method_name
        wrapper.__qualname__ = f"BaseClient.{method_name}"
        setattr(BaseClient, method_name, wrapper)

    undef = _Undefined
    _wrap(
        "get_session",
        lambda s: s.access_token is not undef
        and s.client_id is not undef
        and s.js_pack is not undef
        and s.client_version is not undef,
    )
    _wrap("get_client_token", lambda s: s.client_token is not undef)
    _wrap("get_sha256_hash", lambda s: s.raw_hashes is not undef)

    # part_hash extracts one GraphQL operation hash from the downloaded JS
    # bundles. With the persisted hash map we can usually answer from disk
    # and never download the bundles at all (the ~20-40s startup cost).
    original_part_hash = BaseClient.part_hash

    def part_hash(self: Any, name: str) -> str:
        with _lock:
            if self.client_version is _Undefined and _is_fresh():
                _restore(self)
            if self.client_version is not _Undefined:
                cached = _known_hashes_for(self.client_version).get(name)
                if cached:
                    return cached
            result = original_part_hash(self, name)
            if self.client_version is not _Undefined and result:
                _load_hash_map()
                _hash_map.setdefault(str(self.client_version), {})[name] = result
                _save_hash_map()
            return result

    part_hash.__name__ = "part_hash"
    part_hash.__qualname__ = "BaseClient.part_hash"
    BaseClient.part_hash = part_hash

    _patched = True
    logger.debug("spotapi shared-session cache installed")
    return True


def prewarm_spotapi_session() -> None:
    """
    Perform the first (slow) spotapi session negotiation in a background
    thread so the first user-facing request doesn't pay the ~20s cost.
    """

    def _warm() -> None:
        try:
            from spotapi.client import BaseClient
            from spotapi.http.request import TLSClient
            from spotapi.types.alias import _Undefined

            started = time.time()
            base = BaseClient(client=TLSClient("chrome_120", "", auto_retries=3))
            base.get_client_token()
            # Only download the heavy JS bundles when the persisted hash map
            # doesn't already cover this web-player version
            if (
                base.client_version is _Undefined
                or not _known_hashes_for(base.client_version)
            ):
                base.get_sha256_hash()
            logger.info(
                "Spotify session prewarmed in %.1fs", time.time() - started
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("spotapi prewarm failed (will retry lazily): %s", exc)

    threading.Thread(target=_warm, daemon=True, name="spotapi-prewarm").start()
