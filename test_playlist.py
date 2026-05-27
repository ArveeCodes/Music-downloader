"""Test a single playlist download with the fixed code."""
import json
import time
import urllib.parse
import requests
import threading

SERVER = "http://localhost:8800"
CLIENT_ID = "test_pl_001"

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
    print(f"Client '{CLIENT_ID}' registered")

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

def trigger_download(url, label):
    signals = json.dumps({"client_id": CLIENT_ID, "search_term": url, "loading": False})
    encoded = urllib.parse.quote(signals, safe='')
    endpoint = f"{SERVER}/client/search?datastar={encoded}"
    headers = {"Datastar-Request": "true"}
    print(f"\nTriggering: {label}")
    print(f"  URL: {url}")
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
            print(f"  -> OK, background download started")
            return True
        else:
            print(f"  -> Unexpected response")
            return False
    except Exception as e:
        print(f"  -> ERROR: {e}")
        return False

if __name__ == "__main__":
    print("Creating client...")
    create_client()
    time.sleep(8)

    ka_thread = threading.Thread(target=keep_alive, daemon=True)
    ka_thread.start()
    time.sleep(2)

    # Test Playlist 1 only
    trigger_download(
        "https://open.spotify.com/playlist/6t2HZFCv51gkU6Rhu4W3Dy?si=FP_PGiWhSj-okgbj0GCGtA",
        "Playlist 1"
    )

    # Wait for resolution and a few downloads
    print("\nWaiting 120s for playlist resolution + downloads...")
    time.sleep(120)

    keep_alive_running = False
    print("\nDone. Check server logs.")
