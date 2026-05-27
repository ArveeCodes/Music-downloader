"""Test ALL user-provided URLs through the web server systematically."""
import json
import time
import urllib.parse
import requests
import threading

SERVER = "http://localhost:8800"
CLIENT_ID = "test_batch_001"

def create_client():
    """Hit /client/load to register a client."""
    signals = json.dumps({"client_id": CLIENT_ID})
    encoded = urllib.parse.quote(signals, safe='')
    url = f"{SERVER}/client/load?datastar={encoded}"
    headers = {"Datastar-Request": "true"}
    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=12)
        for i, line in enumerate(resp.iter_lines(decode_unicode=True)):
            if i > 15:
                break
        resp.close()
    except:
        pass
    print(f"[OK] Client '{CLIENT_ID}' registered\n")

def keep_alive():
    """Keep the client alive by periodically hitting /client/load in a background thread."""
    while keep_alive_running:
        try:
            signals = json.dumps({"client_id": CLIENT_ID})
            encoded = urllib.parse.quote(signals, safe='')
            url = f"{SERVER}/client/load?datastar={encoded}"
            headers = {"Datastar-Request": "true"}
            resp = requests.get(url, headers=headers, stream=True, timeout=20)
            for i, line in enumerate(resp.iter_lines(decode_unicode=True)):
                if not keep_alive_running or i > 40:
                    break
                time.sleep(0.5)
            resp.close()
        except:
            pass
        time.sleep(2)

def trigger_download(url, label):
    """Trigger a background download by hitting /client/search with a Spotify URL."""
    signals = json.dumps({"client_id": CLIENT_ID, "search_term": url, "loading": False})
    encoded = urllib.parse.quote(signals, safe='')
    endpoint = f"{SERVER}/client/search?datastar={encoded}"
    headers = {"Datastar-Request": "true"}

    print(f"  Triggering: {label}")
    print(f"    URL: {url}")

    try:
        resp = requests.get(endpoint, headers=headers, stream=True, timeout=120)
        lines = []
        for i, line in enumerate(resp.iter_lines(decode_unicode=True)):
            lines.append(line)
            if i > 20:
                break
        resp.close()

        text = "\n".join(lines)
        if "redirect" in text.lower() or "/downloads" in text:
            print(f"    -> Redirect OK (background download started)")
            return True
        else:
            print(f"    -> Unexpected response (not a redirect)")
            return False
    except requests.exceptions.ReadTimeout:
        print(f"    -> TIMEOUT (server hung for 2min)")
        return False
    except Exception as e:
        print(f"    -> ERROR: {e}")
        return False

if __name__ == "__main__":
    SONGS = [
        ("Song 1 - Kaise Mujhe", "https://open.spotify.com/track/53DXM0bHfB7xkqJZYlksuh?si=1fe8728174584460"),
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
        ("Album 1 (playlist URL)", "https://open.spotify.com/playlist/37i9dQZF1DWTQebjaPYhvs?si=6nlvCUdRRa6V4a7WejpBJg"),
        ("Album 2", "https://open.spotify.com/album/5chTLnnxlxQVAgEv6YbEBe?si=5eAEsALKSuG75L4szUBMng"),
        ("Album 3", "https://open.spotify.com/album/63yRRBtLX8eqbOLD6f0y9U?si=aHIx450kQaSUhjvSwELQqA"),
        ("Album 4", "https://open.spotify.com/album/4SZko61aMnmgvNhfhgTuD3?si=mk80Gk2wRN-8wfGqCKnQMw"),
    ]

    print("="*60)
    print("CREATING CLIENT")
    print("="*60)
    create_client()
    time.sleep(5)

    # Start keep-alive thread
    keep_alive_running = True
    ka_thread = threading.Thread(target=keep_alive, daemon=True)
    ka_thread.start()
    time.sleep(2)

    # Test songs (one at a time, 5s gap)
    print("="*60)
    print("TESTING SONGS (4 tracks)")
    print("="*60)
    for label, url in SONGS:
        trigger_download(url, label)
        time.sleep(3)

    print()
    print("="*60)
    print("TESTING PLAYLISTS (4 playlists)")
    print("="*60)
    for label, url in PLAYLISTS:
        trigger_download(url, label)
        time.sleep(3)

    print()
    print("="*60)
    print("TESTING ALBUMS (4 albums)")
    print("="*60)
    for label, url in ALBUMS:
        trigger_download(url, label)
        time.sleep(3)

    print()
    print("="*60)
    print("ALL DOWNLOADS TRIGGERED")
    print("Waiting 30s for downloads to start processing...")
    print("="*60)
    time.sleep(30)

    keep_alive_running = False
    print("\nDone. Check server logs for download progress.")
