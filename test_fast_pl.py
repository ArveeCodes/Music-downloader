"""Test fast playlist resolution with fetch_songs=False."""
import json
import time
import urllib.parse
import requests
import threading

SERVER = "http://localhost:8800"
CLIENT_ID = "test_fast_001"

def create_client():
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
    print(f"Client registered")

keep_alive_running = True

def keep_alive():
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

def trigger(url, label):
    signals = json.dumps({"client_id": CLIENT_ID, "search_term": url, "loading": False})
    encoded = urllib.parse.quote(signals, safe='')
    endpoint = f"{SERVER}/client/search?datastar={encoded}"
    headers = {"Datastar-Request": "true"}
    t_start = time.time()
    print(f"\n[{label}] Triggering...")
    try:
        resp = requests.get(endpoint, headers=headers, stream=True, timeout=120)
        lines = []
        for i, line in enumerate(resp.iter_lines(decode_unicode=True)):
            lines.append(line)
            if i > 20:
                break
        resp.close()
        text = "\n".join(lines)
        elapsed = time.time() - t_start
        if "redirect" in text.lower() or "/downloads" in text:
            print(f"[{label}] OK - redirect in {elapsed:.1f}s")
            return True
        else:
            print(f"[{label}] Unexpected: {text[:100]}")
            return False
    except Exception as e:
        print(f"[{label}] ERROR: {e}")
        return False

if __name__ == "__main__":
    create_client()
    time.sleep(8)

    ka_thread = threading.Thread(target=keep_alive, daemon=True)
    ka_thread.start()
    time.sleep(2)

    # Test all 12 URLs sequentially with timing
    ALL_URLS = [
        ("Song1", "https://open.spotify.com/track/53DXM0bHfB7xkqJZYlksuh?si=1fe8728174584460"),
        ("Song2", "https://open.spotify.com/track/3yHyiUDJdz02FZ6jfUbsmY?si=bff8cb0aae59482a"),
        ("Song3", "https://open.spotify.com/track/2R4zzmPXIFuBH0BqdWDkfC?si=7929b88a6ac94162"),
        ("Song4", "https://open.spotify.com/track/1wnozUBSe7OMpmZiTPJC5j?si=57b1f5af026c496b"),
        ("Playlist1", "https://open.spotify.com/playlist/6t2HZFCv51gkU6Rhu4W3Dy?si=FP_PGiWhSj-okgbj0GCGtA"),
        ("Playlist2", "https://open.spotify.com/playlist/78Or0eeZU2HdQ3u2uU4wxZ?si=TtJlRAfQRJm_BPWyrKfrQQ"),
        ("Playlist3", "https://open.spotify.com/playlist/2aKb4Jb2KRRLJY3ogIzP1q?si=w9f3NoaERuiFlqoEVn7LgQ"),
        ("Playlist4", "https://open.spotify.com/playlist/0UFLdxvi1WNVHy5jdSmjEO?si=Jb0A7-mhSK-QwvKYShLRBQ"),
        ("Album1-PL", "https://open.spotify.com/playlist/37i9dQZF1DWTQebjaPYhvs?si=6nlvCUdRRa6V4a7WejpBJg"),
        ("Album2", "https://open.spotify.com/album/5chTLnnxlxQVAgEv6YbEBe?si=5eAEsALKSuG75L4szUBMng"),
        ("Album3", "https://open.spotify.com/album/63yRRBtLX8eqbOLD6f0y9U?si=aHIx450kQaSUhjvSwELQqA"),
        ("Album4", "https://open.spotify.com/album/4SZko61aMnmgvNhfhgTuD3?si=mk80Gk2wRN-8wfGqCKnQMw"),
    ]

    for label, url in ALL_URLS:
        trigger(url, label)
        time.sleep(2)

    # Let downloads run for 5 minutes
    print("\n\nAll triggered. Waiting 5 min for downloads...")
    time.sleep(300)

    keep_alive_running = False
    print("Done. Check server logs for results.")
