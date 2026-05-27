"""
Test script to systematically test each download URL through the running web server.
Simulates what the browser does: hit /client/search with the URL as search_term.
"""
import json
import time
import urllib.parse
import requests

SERVER = "http://localhost:8800"

# First, establish a client by hitting /client/load
def get_or_create_client(client_id="test_client_001"):
    """Hit /client/load to register a client, read the SSE stream for client_id."""
    signals = json.dumps({"client_id": client_id})
    encoded = urllib.parse.quote(signals)
    url = f"{SERVER}/client/load?datastar={encoded}"
    try:
        resp = requests.get(url, stream=True, timeout=10)
        lines = []
        for i, line in enumerate(resp.iter_lines(decode_unicode=True)):
            lines.append(line)
            if i > 15:  # Read enough to get the client_id patch
                break
        resp.close()
        print(f"[OK] Client load responded. First lines:")
        for l in lines[:10]:
            print(f"  {l}")
        return client_id
    except Exception as e:
        print(f"[ERROR] Client load failed: {e}")
        return client_id


def test_search_url(client_id, url, label=""):
    """
    Hit /client/search with a Spotify URL.
    For valid URLs, the server should fire background_download and redirect to /downloads.
    """
    signals = json.dumps({
        "client_id": client_id,
        "search_term": url,
        "loading": False
    })
    encoded = urllib.parse.quote(signals)
    endpoint = f"{SERVER}/client/search?datastar={encoded}"

    print(f"\n{'='*60}")
    print(f"Testing: {label}")
    print(f"URL: {url}")
    print(f"{'='*60}")

    try:
        resp = requests.get(endpoint, stream=True, timeout=60)
        lines = []
        for i, line in enumerate(resp.iter_lines(decode_unicode=True)):
            lines.append(line)
            if i > 20:
                break
        resp.close()

        print(f"Status: {resp.status_code}")
        for l in lines:
            if l.strip():
                print(f"  {l}")

        # Check for redirect (means URL was detected and download started)
        response_text = "\n".join(lines)
        if "redirect" in response_text.lower() or "/downloads" in response_text:
            print(f"[OK] Redirect to downloads detected - background download started")
            return True
        elif "error" in response_text.lower():
            print(f"[ERROR] Error in response")
            return False
        else:
            print(f"[INFO] Got search results (not a URL redirect)")
            return True

    except requests.exceptions.Timeout:
        print(f"[ERROR] Request timed out after 60s")
        return False
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        return False


def test_direct_download(client_id, url, label=""):
    """
    Hit /client/download/ with a song URL (POST).
    This is what the download button does in search results.
    """
    signals = json.dumps({
        "client_id": client_id,
        "song_url": url,
        "search_term": "",
        "loading": False
    })
    encoded = urllib.parse.quote(signals)
    endpoint = f"{SERVER}/client/download/"

    print(f"\n{'='*60}")
    print(f"Direct download: {label}")
    print(f"URL: {url}")
    print(f"{'='*60}")

    try:
        resp = requests.post(
            endpoint,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=f"datastar={encoded}",
            stream=True,
            timeout=120
        )
        lines = []
        for i, line in enumerate(resp.iter_lines(decode_unicode=True)):
            lines.append(line)
            if i > 30:
                break
        resp.close()

        print(f"Status: {resp.status_code}")
        for l in lines:
            if l.strip():
                print(f"  {l}")
        return resp.status_code == 200
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    # Songs
    SONGS = [
        ("Song 1", "https://open.spotify.com/track/53DXM0bHfB7xkqJZYlksuh?si=1fe8728174584460"),
        ("Song 2", "https://open.spotify.com/track/3yHyiUDJdz02FZ6jfUbsmY?si=bff8cb0aae59482a"),
        ("Song 3", "https://open.spotify.com/track/2R4zzmPXIFuBH0BqdWDkfC?si=7929b88a6ac94162"),
        ("Song 4", "https://open.spotify.com/track/1wnozUBSe7OMpmZiTPJC5j?si=57b1f5af026c496b"),
    ]

    PLAYLISTS = [
        ("Playlist 1", "https://open.spotify.com/playlist/6t2HZFCv51gkU6Rhu4W3Dy?si=FP_PGiWhSj-okgbj0GCGtA"),
        ("Playlist 2", "https://open.spotify.com/playlist/78Or0eeZU2HdQ3u2uU4wxZ?si=TtJlRAfQRJm_BPWyrKfrQQ"),
        ("Playlist 3", "https://open.spotify.com/playlist/2aKb4Jb2KRRLJY3ogIzP1q?si=w9f3NoaERuiFlqoEVn7LgQ"),
        ("Playlist 4", "https://open.spotify.com/playlist/0UFLdxvi1WNVHy5jdSmjEO?si=Jb0A7-mhSK-QwvKYShLRBQ"),
    ]

    ALBUMS = [
        ("Album 1 (actually playlist)", "https://open.spotify.com/playlist/37i9dQZF1DWTQebjaPYhvs?si=6nlvCUdRRa6V4a7WejpBJg"),
        ("Album 2", "https://open.spotify.com/album/5chTLnnxlxQVAgEv6YbEBe?si=5eAEsALKSuG75L4szUBMng"),
        ("Album 3", "https://open.spotify.com/album/63yRRBtLX8eqbOLD6f0y9U?si=aHIx450kQaSUhjvSwELQqA"),
        ("Album 4", "https://open.spotify.com/album/4SZko61aMnmgvNhfhgTuD3?si=mk80Gk2wRN-8wfGqCKnQMw"),
    ]

    print("="*60)
    print("STEP 1: Create/connect client")
    print("="*60)
    client_id = get_or_create_client()
    time.sleep(3)  # Let client initialize

    print("\n\n" + "="*60)
    print("STEP 2: Test Song #1 (single track)")
    print("="*60)
    test_search_url(client_id, SONGS[0][1], SONGS[0][0])
