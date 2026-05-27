"""Test download URLs through the web server with proper Datastar headers."""
import json
import sys
import time
import urllib.parse
import requests

SERVER = "http://localhost:8800"

def create_client(client_id="test_auto_001"):
    """Connect a client via /client/load SSE endpoint."""
    signals = json.dumps({"client_id": client_id})
    encoded = urllib.parse.quote(signals, safe='')
    url = f"{SERVER}/client/load?datastar={encoded}"
    headers = {"Datastar-Request": "true"}
    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=15)
        for i, line in enumerate(resp.iter_lines(decode_unicode=True)):
            if "patch-signals" in str(line) or i > 20:
                break
        resp.close()
        print(f"[OK] Client '{client_id}' registered")
        return True
    except Exception as e:
        print(f"[WARN] Client load stream closed: {e}")
        return True  # Client was still registered on server side

def test_search_url(client_id, url, label):
    """Test /client/search with a Spotify URL. Valid URLs trigger background download + redirect."""
    signals = json.dumps({"client_id": client_id, "search_term": url, "loading": False})
    encoded = urllib.parse.quote(signals, safe='')
    endpoint = f"{SERVER}/client/search?datastar={encoded}"
    headers = {"Datastar-Request": "true"}

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  {url}")
    print(f"{'='*60}")

    try:
        resp = requests.get(endpoint, headers=headers, stream=True, timeout=90)
        lines = []
        for i, line in enumerate(resp.iter_lines(decode_unicode=True)):
            lines.append(line)
            if i > 30:
                break
        resp.close()

        text = "\n".join(lines)
        if "redirect" in text.lower() or "/downloads" in text:
            print(f"  [OK] URL detected -> redirect to /downloads (background download started)")
            return "redirect"
        elif "No results found" in text:
            print(f"  [FAIL] URL was NOT detected - fell through to search, no results")
            return "no_results"
        elif "search-list" in text:
            print(f"  [OK] Search results returned")
            return "search_results"
        else:
            print(f"  [??] Unknown response:")
            for l in lines[:10]:
                if l.strip():
                    print(f"    {l}")
            return "unknown"
    except requests.exceptions.ReadTimeout:
        print(f"  [TIMEOUT] Request timed out - server may be processing")
        return "timeout"
    except Exception as e:
        print(f"  [ERROR] {type(e).__name__}: {e}")
        return "error"

if __name__ == "__main__":
    client_id = "test_auto_001"

    print("STEP 1: Register client")
    create_client(client_id)
    time.sleep(8)  # Let Downloader initialize

    # Test just the first song
    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://open.spotify.com/track/53DXM0bHfB7xkqJZYlksuh?si=1fe8728174584460"
    test_label = sys.argv[2] if len(sys.argv) > 2 else "Song 1"

    result = test_search_url(client_id, test_url, test_label)
    print(f"\nResult: {result}")
