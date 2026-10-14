
<!--- mdformat-toc start --slug=github --->

<div align="center">

# spotDL v4

**spotDL** finds songs from Spotify playlists on YouTube and downloads them - along with album art, lyrics and metadata.

[![MIT License](https://img.shields.io/github/license/spotdl/spotify-downloader?color=44CC11&style=flat-square)](https://github.com/spotDL/spotify-downloader/blob/master/LICENSE)
[![PyPI version](https://img.shields.io/pypi/pyversions/spotDL?color=%2344CC11&style=flat-square)](https://pypi.org/project/spotdl/)
[![PyPi downloads](https://img.shields.io/pypi/dw/spotDL?label=downloads@pypi&color=344CC11&style=flat-square)](https://pypi.org/project/spotdl/)
![Contributors](https://img.shields.io/github/contributors/spotDL/spotify-downloader?style=flat-square)
[![Discord](https://img.shields.io/discord/771628785447337985?label=discord&logo=discord&style=flat-square)](https://discord.gg/xCa23pwJWY)

> The fastest, easiest and most accurate command-line music downloader.

</div>

______________________________________________________________________
**[Read the documentation on ReadTheDocs!](https://spotdl.readthedocs.io)**
______________________________________________________________________

## Features

- Download songs, albums, playlists, and artist discographies from Spotify
- Embeds album art, lyrics, and full metadata automatically
- Multiple audio providers: YouTube, YouTube Music, SoundCloud, Bandcamp, Piped
- Multiple lyrics providers: Genius, Musixmatch, AZLyrics, Synced
- Built-in web UI for browser-based downloading
- Sync mode to keep local directories in sync with Spotify playlists
- Configurable output format (mp3, flac, ogg, opus, m4a, wav)

## Web UI

spotDL includes a modern web interface built with Datastar, Tailwind CSS 4, and DaisyUI 5.

```
py -m spotdl web
```

This starts a local server at `http://localhost:8800` with:

- **Search** - Find songs on Spotify and download them with one click
- **URL support** - Paste any Spotify track, album, playlist, or artist URL
- **Live download progress** - Real-time progress bars via server-sent events
- **Settings** - Configure audio/lyrics providers, output format, and more from the browser
- **Dark/light theme** - Toggle between themes with the navbar switch
- **Responsive design** - Works on desktop and mobile

### Performance

The web UI includes several optimizations for speed:

- **Fast search** - Constructs song metadata directly from Spotify search results, eliminating redundant API calls (1 API call per search instead of 31)
- **Search caching** - Results are cached for 5 minutes to avoid repeated API hits
- **Request deduplication** - Concurrent searches and downloads for the same item are coalesced into a single request
- **Audio provider fallback** - If one provider fails, automatically tries the next
- **Adaptive polling** - SSE update intervals scale based on activity

## Installation

Refer to our [Installation Guide](docs/installation.md) for more details.

### Python (Recommended Method)

- _spotDL_ can be installed by running `pip install spotdl`.
- To update spotDL run `pip install --upgrade spotdl`

  > On some systems you might have to change `pip` to `pip3`.

<details>
    <summary style="font-size:1.25em"><strong>Other options</strong></summary>

- Prebuilt executable
  - You can download the latest version from the
    [Releases Tab](https://github.com/spotDL/spotify-downloader/releases)
- On Termux
  - `curl -L https://raw.githubusercontent.com/spotDL/spotify-downloader/master/scripts/termux.sh | sh`
- Arch
  - There is an [Arch User Repository (AUR) package](https://aur.archlinux.org/packages/spotdl/) for
    spotDL.
- Docker
  - Build image:

    ```bash
    docker build -t spotdl .
    ```

  - Launch container with spotDL parameters (see section below). You need to create mapped
    volume to access song files

    ```bash
    docker run --rm -v $(pwd):/music spotdl download [trackUrl]
    ```

  - For Docker Compose and permission-managed Docker downloads, see
    [the Docker section in `/docs/index.md`](docs/index.md#docker).

  - Build from source

    ```bash
    git clone https://github.com/spotDL/spotify-downloader && cd spotify-downloader
    pip install uv
    uv sync
    uv run scripts/build.py
    ```

    An executable is created in `spotify-downloader/dist/`.

</details>

### Installing FFmpeg

FFmpeg is required for spotDL. If using FFmpeg only for spotDL, you can simply install FFmpeg to your spotDL installation directory:
`spotdl --download-ffmpeg`

We recommend the above option, but if you want to install FFmpeg system-wide,
follow these instructions

- [Windows Tutorial](https://windowsloop.com/install-ffmpeg-windows-10/)
- OSX - `brew install ffmpeg`
- Linux - `sudo apt install ffmpeg` or use your distro's package manager

### Installing Deno

We strongly recommend installing Deno. spotDL uses yt-dlp for YouTube downloads, and some
videos require Deno to download successfully. Without Deno, spotDL may fail to download some
songs, including videos marked as "made for kids".

If using Deno only for spotDL, install Deno to your spotDL directory:
`spotdl --download-deno`

If you want to install Deno system-wide instead, follow the
[official Deno installation guide](https://docs.deno.com/runtime/getting_started/installation/).

## Usage

Using SpotDL without options:

```sh
spotdl [urls]
```

You can run _spotDL_ as a package if running it as a script doesn't work:

```sh
python -m spotdl [urls]
```

General usage:

```sh
spotdl [operation] [options] QUERY
```

There are different **operations** spotDL can perform. The _default_ is `download`, which simply downloads the songs from YouTube and embeds metadata.

The **query** for spotDL is usually a list of Spotify URLs, but for some operations like **sync**, only a single link or file is required.
For a list of all **options** use ```spotdl -h```

<details>
<summary style="font-size:1em"><strong>Supported operations</strong></summary>

- `download`: Downloads songs from Spotify URLs or search queries (default operation).

- `save`: Saves only the metadata from Spotify without downloading anything.
    - Usage:
        `spotdl save [query] --save-file {filename}.spotdl`

- `web`: Starts a web interface instead of using the command line.
    - Usage:
        `spotdl web`

- `url`: Get user-friendly URL for each song from the query.
    - Usage:
        `spotdl url [query]`

- `sync`: Updates directories. Compares the directory with the current state of the playlist. Newly added songs will be downloaded and removed songs will be deleted. No other songs will be downloaded and no other files will be deleted.

    - Usage:
        `spotdl sync [query] --save-file {filename}.spotdl`

        This creates a new **sync** file. To update the directory in the future, use:

        `spotdl sync {filename}.spotdl`

- `meta`: Updates metadata for the provided song files.

</details>

## Music Sourcing and Audio Quality

spotDL uses YouTube as a source for music downloads. This method is used to avoid any issues related to downloading music from Spotify.

> **Note**
> Users are responsible for their actions and potential legal consequences. We do not support unauthorized downloading of copyrighted material and take no responsibility for user actions.

### Audio Quality

spotDL downloads music from YouTube and is designed to always download the highest possible bitrate; which is 128 kbps for regular users and 256 kbps for YouTube Music premium users.

Check the [Audio Formats](docs/usage.md#audio-formats-and-quality) page for more info.

## Contributing

Interested in contributing? Check out our [CONTRIBUTING.md](docs/CONTRIBUTING.md) to find
resources around contributing along with a guide on how to set up a development environment.

### Join our amazing community as a code contributor

<a href="https://github.com/spotDL/spotify-downloader/graphs/contributors">
  <img class="dark-light" src="https://contrib.rocks/image?repo=spotDL/spotify-downloader&anon=0&columns=25&max=100&r=true" />
</a>

## License

This project is Licensed under the [MIT](/LICENSE) License.
