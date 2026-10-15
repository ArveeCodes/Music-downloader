<div align="center">

<img src="spotdl/web/static/assets/spotdl.svg" width="84" alt="Kraken logo" />

# Kraken

**Paste a link. Keep the music.**

Download Spotify songs, albums, playlists and artists with cover art, lyrics and full metadata, from a clean web app or the command line.

[![License: MIT](https://img.shields.io/badge/license-MIT-5b9bff?style=flat-square)](LICENSE)
[![Python 3.10 – 3.14](https://img.shields.io/badge/python-3.10%20%E2%80%93%203.14-5b9bff?style=flat-square)](https://www.python.org/downloads/)

</div>

---

## Highlights

- **Search everything**: one search returns matching songs, albums and artists.
- **Paste any Spotify link**: track, album, playlist or artist. No extra steps.
- **Live progress**: watch each download climb to 100%, with a clear done, partial or failed result.
- **Organized automatically**: albums, playlists and artist catalogs each get their own folder.
- **Full metadata**: cover art, lyrics (when available), artist, album and track numbers embedded in every file.
- **Fast**: about one second per search, and links resolve in about a second once the session is warm.

## Quick start

Kraken is built on spotDL, so the package and the commands are still called `spotdl`.

**1. Install** (Python 3.10–3.14)

```bash
git clone -b main https://github.com/ArveeCodes/spotify-downloader.git
cd spotify-downloader
pip install -e .
```

**2. Install FFmpeg** (required), and Deno (recommended, some downloads need it)

```bash
python -m spotdl --download-ffmpeg
python -m spotdl --download-deno
```

**3. Start the web app**

```bash
python -m spotdl web
```

Then open **http://localhost:8800**.

## Using the web app

| Page | What it does |
| --- | --- |
| **Home** | Search, or paste a Spotify link and press Enter. |
| **Search results** | Artists, albums and songs in separate sections. Each row has a download button and a link to open it in Spotify. |
| **Link download** | Pasting a link shows a live card with progress, a song counter for collections, and the final result. |
| **Downloads** | Everything downloaded in this session, with progress and a save button for finished files. |
| **Settings** (sliders icon) | Audio providers, lyrics providers and output format. |

The dot in the top bar shows the connection: blue when connected, red when disconnected (click it to reconnect).

## Where downloads are saved

Downloads go to the folder set in your config file:

- **Windows:** `C:\Users\<you>\.spotdl\config.json`
- **macOS / Linux:** `~/.spotdl/config.json`

To save straight to a folder on disk, set these two keys:

```json
"web_use_output_dir": true,
"output": "E:/Music Downloader/downloads/{artists} - {title}.{output-ext}"
```

- Single songs land directly in that folder.
- Albums, playlists and artists get their own subfolder, e.g. `downloads/After Hours/`.
- `output` also applies to the command line, so both save to the same place.
- With `web_use_output_dir` set to `false`, files go to a temporary folder that's cleared when the server stops, and you save them from the Downloads page instead.

Restart the server after changing the config. If you don't have a config file yet, `python -m spotdl --generate-config` creates one (it overwrites an existing file).

See [docs/usage.md](docs/usage.md) for all output template variables.

## Command line

```bash
spotdl [operation] [options] QUERY
```

The default operation is `download`, and the query is usually one or more Spotify links:

```bash
spotdl download https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj
```

| Operation | Purpose |
| --- | --- |
| `download` | Download songs from Spotify links or search queries (default). |
| `web` | Start the web app. |
| `save` | Save metadata only: `spotdl save [query] --save-file name.spotdl` |
| `sync` | Keep a folder in sync with a playlist: `spotdl sync [query] --save-file name.spotdl`, then later `spotdl sync name.spotdl` |
| `url` | Get a user-friendly URL for each song in the query. |
| `meta` | Update metadata for existing song files. |

Run `spotdl -h` for every option. More detail is in [docs/usage.md](docs/usage.md) and [docs/installation.md](docs/installation.md); for problems see [docs/troubleshooting.md](docs/troubleshooting.md).

## Project layout

```
spotdl/
├── web/
│   ├── routes.py          # Pages and live (SSE) endpoints: search, link downloads, progress
│   ├── api.py             # REST endpoints, including file downloads
│   ├── components/        # Jinja2 templates (UI)
│   └── static/assets/     # Logo, Datastar, Tailwind, DaisyUI
├── utils/
│   ├── spotapi_cache.py   # Shares one Spotify session across requests (big speedup)
│   ├── web.py             # Client sessions and app state
│   └── spotify.py         # Spotify client setup
└── download/
    └── downloader.py      # Matching, downloading and tagging
```

The web UI is server-rendered: Jinja2 templates, with live updates patched in over server-sent events by [Datastar](https://data-star.dev). Styling uses Tailwind CSS 4 and DaisyUI 5.

### Performance notes

- **One API call per search**: results are built straight from the search response instead of re-fetching every track, then cached for five minutes.
- **Shared Spotify session**: the session is negotiated once and reused. Its query hashes are saved to `~/.spotdl/spotapi-hashes.json`, so restarts are fast too.
- **Quiet live updates**: progress streams only send a patch when something actually changed.

## Legal

Kraken does not download audio from Spotify. It matches each track with an audio source from the configured providers and tags the file with Spotify's metadata.

> **Note**
> Users are responsible for their actions and any legal consequences. We do not support unauthorized downloading of copyrighted material and take no responsibility for how this software is used.

## Credits and license

Built on [spotDL](https://github.com/spotDL/spotify-downloader) by the spotDL developers. Licensed under the [MIT License](LICENSE).

Want to contribute? See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md).
