"""
Module for handling client signals with input validation.
"""

import re
from typing import Optional

from spotdl.types.options import DownloaderOptions
from spotdl.utils.web import app_state

# Valid client_id: hex string (UUID without dashes), 1-64 chars
_CLIENT_ID_RE = re.compile(r'^[a-f0-9]{1,64}$')


class Signals:
    """
    Class that holds the client datastar signals.
    """

    client_id: str = ""
    search_term: str = ""
    downloader_settings: Optional["DownloaderOptions"] = None
    song_url: str = ""


def _sanitize_log_string(value: str) -> str:
    """Strip control characters to prevent log injection."""
    return value.replace('\n', ' ').replace('\r', ' ').replace('\x00', '')


def handle_signals(datastar_signals: dict | None) -> Signals:
    """
    Handle the signals received from the client.
    Validates and sanitizes input to prevent injection attacks.
    """
    app_state.logger.debug(f"Received signals: {datastar_signals}")
    if not datastar_signals:
        app_state.logger.warning("No signals provided, returning empty Signals.")
        return Signals()

    signals = Signals()

    # Validate client_id format (must be hex string for path safety)
    raw_client_id = str(datastar_signals.get("client_id", ""))
    if raw_client_id and not _CLIENT_ID_RE.match(raw_client_id):
        app_state.logger.warning(
            f"Invalid client_id format: {_sanitize_log_string(raw_client_id)!r}"
        )
        signals.client_id = ""
    else:
        signals.client_id = raw_client_id

    # Sanitize search_term for logging (keep original for search)
    signals.search_term = str(datastar_signals.get("search_term", ""))
    signals.downloader_settings = datastar_signals.get("downloader_settings", {})
    signals.song_url = str(datastar_signals.get("song_url", ""))

    return signals
