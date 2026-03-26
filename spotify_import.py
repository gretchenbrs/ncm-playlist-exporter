#!/usr/bin/env python3
import argparse
import base64
import csv
import json
import os
import random
import time
import urllib.parse
import urllib.request
import urllib.error
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
SCOPES = "playlist-modify-private playlist-modify-public"
MAX_RETRIES = 6
TOKEN_CACHE_FILE = Path(".spotify_token_cache.json")
MAX_SINGLE_WAIT_SECONDS = 300


def http_post(url: str, data: dict, headers: dict | None = None) -> dict:
    payload = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < MAX_RETRIES - 1:
                retry_after = e.headers.get("Retry-After")
                wait_s = float(retry_after) if retry_after else (1.5 * (attempt + 1))
                if wait_s > MAX_SINGLE_WAIT_SECONDS:
                    raise RuntimeError(
                        f"Spotify rate-limit window too long ({wait_s:.0f}s). "
                        "Stop now and rerun later; progress is resumable."
                    ) from e
                wait_s += random.uniform(0.0, 0.4)
                print(f"Rate limited (429). Retrying in {wait_s:.1f}s ...")
                time.sleep(wait_s)
                continue
            raise RuntimeError(f"HTTP {e.code} on POST {url}: {detail}") from e


def http_get(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, method="GET")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < MAX_RETRIES - 1:
                retry_after = e.headers.get("Retry-After")
                wait_s = float(retry_after) if retry_after else (1.5 * (attempt + 1))
                if wait_s > MAX_SINGLE_WAIT_SECONDS:
                    raise RuntimeError(
                        f"Spotify rate-limit window too long ({wait_s:.0f}s). "
                        "Stop now and rerun later; progress is resumable."
                    ) from e
                wait_s += random.uniform(0.0, 0.4)
                print(f"Rate limited (429). Retrying in {wait_s:.1f}s ...")
                time.sleep(wait_s)
                continue
            raise RuntimeError(f"HTTP {e.code} on GET {url}: {detail}") from e


def http_post_json(url: str, body: dict, headers: dict) -> dict:
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)

    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < MAX_RETRIES - 1:
                retry_after = e.headers.get("Retry-After")
                wait_s = float(retry_after) if retry_after else (1.5 * (attempt + 1))
                if wait_s > MAX_SINGLE_WAIT_SECONDS:
                    raise RuntimeError(
                        f"Spotify rate-limit window too long ({wait_s:.0f}s). "
                        "Stop now and rerun later; progress is resumable."
                    ) from e
                wait_s += random.uniform(0.0, 0.4)
                print(f"Rate limited (429). Retrying in {wait_s:.1f}s ...")
                time.sleep(wait_s)
                continue
            raise RuntimeError(f"HTTP {e.code} on POST {url}: {detail}") from e


class OAuthCodeHandler(BaseHTTPRequestHandler):
    code = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)

        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        OAuthCodeHandler.code = (q.get("code") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h3>Spotify authorization complete. You can close this window.</h3>".encode("utf-8"))

    def log_message(self, format, *args):
        return


def read_tracks_from_csv(csv_path: Path) -> list[dict]:
    tracks = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = (row.get("title") or "").strip()
            artists = (row.get("artists") or "").strip()
            album = (row.get("album") or "").strip()
            if not title:
                continue
            tracks.append({"title": title, "artists": artists, "album": album})
    return tracks


def build_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("utf-8")


def load_token_cache(client_id: str) -> dict | None:
    if not TOKEN_CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("client_id") != client_id:
        return None
    return payload


def save_token_cache(client_id: str, token: dict) -> None:
    payload = {
        "client_id": client_id,
        "access_token": token.get("access_token"),
        "refresh_token": token.get("refresh_token"),
        "expires_at": int(time.time()) + int(token.get("expires_in", 3600)) - 30,
    }
    TOKEN_CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    return http_post(
        TOKEN_URL,
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={"Authorization": build_auth_header(client_id, client_secret)},
    )


def exchange_code_token_raw(client_id: str, client_secret: str, redirect_uri: str, code: str) -> dict:
    return http_post(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"Authorization": build_auth_header(client_id, client_secret)},
    )


def get_access_token(client_id: str, client_secret: str, redirect_uri: str, callback_url: str | None = None) -> str:
    cached = load_token_cache(client_id)
    if cached and cached.get("access_token") and int(cached.get("expires_at", 0)) > int(time.time()):
        print("Using cached Spotify token.")
        return str(cached["access_token"])
    if cached and cached.get("refresh_token"):
        try:
            refreshed = refresh_access_token(client_id, client_secret, str(cached["refresh_token"]))
            if "refresh_token" not in refreshed:
                refreshed["refresh_token"] = cached["refresh_token"]
            save_token_cache(client_id, refreshed)
            print("Refreshed Spotify token.")
            return str(refreshed["access_token"])
        except Exception:
            pass

    if callback_url:
        code = extract_code_from_callback_url(callback_url)
        if not code:
            raise RuntimeError("Invalid --callback-url. Expected URL containing ?code=...")
        token = exchange_code_token_raw(client_id, client_secret, redirect_uri, code)
        save_token_cache(client_id, token)
        return str(token["access_token"])

    OAuthCodeHandler.code = None
    state = str(int(time.time()))
    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
        "show_dialog": "true",
    }

    auth_link = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print("Open this URL in your browser and approve:")
    print(auth_link)

    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8888

    try:
        webbrowser.open(auth_link)
    except Exception:
        pass

    server = HTTPServer((host, port), OAuthCodeHandler)
    server.timeout = 1.0
    deadline = time.time() + 120
    print(f"Waiting for callback on {host}:{port} ...")
    while OAuthCodeHandler.code is None and time.time() < deadline:
        server.handle_request()
    server.server_close()

    if not OAuthCodeHandler.code:
        print("No callback captured automatically.")
        print("Paste the full callback URL from your browser (http://127.0.0.1:8888/callback?code=...):")
        pasted = input("> ").strip()
        code = extract_code_from_callback_url(pasted)
        if not code:
            raise RuntimeError("No valid callback URL provided.")
        OAuthCodeHandler.code = code

    token = exchange_code_token_raw(client_id, client_secret, redirect_uri, str(OAuthCodeHandler.code))

    access = token.get("access_token")
    if not access:
        raise RuntimeError(f"Token exchange failed: {token}")
    save_token_cache(client_id, token)
    return access


def exchange_code_for_token(client_id: str, client_secret: str, redirect_uri: str, code: str) -> str:
    token = exchange_code_token_raw(client_id, client_secret, redirect_uri, code)

    access = token.get("access_token")
    if not access:
        raise RuntimeError(f"Token exchange failed: {token}")
    save_token_cache(client_id, token)
    return access


def extract_code_from_callback_url(callback_url: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(callback_url.strip())
        q = urllib.parse.parse_qs(parsed.query)
        return (q.get("code") or [None])[0]
    except Exception:
        return None


def spotify_get_me(access_token: str) -> dict:
    return http_get(f"{API_BASE}/me", headers={"Authorization": f"Bearer {access_token}"})


def spotify_search_track(access_token: str, title: str, artists: str) -> str | None:
    q = f'track:"{title}"'
    first_artist = artists.split(",")[0].strip() if artists else ""
    if first_artist:
        q += f' artist:"{first_artist}"'

    params = urllib.parse.urlencode({"q": q, "type": "track", "limit": 5})
    data = http_get(f"{API_BASE}/search?{params}", headers={"Authorization": f"Bearer {access_token}"})
    items = ((data.get("tracks") or {}).get("items") or [])
    if not items:
        return None

    return items[0].get("uri")


def spotify_create_playlist(access_token: str, name: str, description: str, public: bool) -> str:
    payload = {"name": name, "description": description, "public": public}
    data = http_post_json(
        f"{API_BASE}/me/playlists",
        payload,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    playlist_id = data.get("id")
    if not playlist_id:
        raise RuntimeError(f"Create playlist failed: {data}")
    return playlist_id


def spotify_add_items(access_token: str, playlist_id: str, uris: list[str]) -> None:
    for i in range(0, len(uris), 100):
        chunk = uris[i : i + 100]
        http_post_json(
            f"{API_BASE}/playlists/{playlist_id}/tracks",
            {"uris": chunk},
            headers={"Authorization": f"Bearer {access_token}"},
        )


def write_unmatched(unmatched: list[dict], out_path: Path):
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["title", "artists", "album"])
        writer.writeheader()
        writer.writerows(unmatched)


def load_progress(progress_path: Path, total: int) -> list[str | None]:
    if not progress_path.exists():
        return [None] * total
    try:
        payload = json.loads(progress_path.read_text(encoding="utf-8"))
        items = payload.get("items") or []
        if not isinstance(items, list) or len(items) != total:
            return [None] * total
        return [x if isinstance(x, str) or x is None else None for x in items]
    except Exception:
        return [None] * total


def save_progress(progress_path: Path, items: list[str | None]) -> None:
    payload = {"items": items, "updatedAt": int(time.time())}
    progress_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import exported NetEase CSV into Spotify playlist")
    parser.add_argument("--csv", required=True, help="Path to exported CSV")
    parser.add_argument("--name", help="Target Spotify playlist name")
    parser.add_argument("--public", action="store_true", help="Create public playlist (default private)")
    parser.add_argument("--redirect-uri", default=os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"))
    parser.add_argument("--throttle-ms", type=int, default=80, help="Delay between track search requests in milliseconds")
    parser.add_argument(
        "--callback-url",
        help="Optional pasted callback URL (http://127.0.0.1:8888/callback?code=...) to bypass local callback listener",
    )
    args = parser.parse_args()

    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print("Missing env vars: SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET")
        return 1

    csv_path = Path(args.csv).resolve()
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        return 1

    tracks = read_tracks_from_csv(csv_path)
    if not tracks:
        print("No tracks found in CSV")
        return 1

    print(f"Loaded {len(tracks)} tracks from CSV")

    access_token = get_access_token(client_id, client_secret, args.redirect_uri, args.callback_url)
    me = spotify_get_me(access_token)
    user_id = me.get("id")
    if not user_id:
        print("Failed to fetch Spotify user profile")
        return 1

    playlist_name = args.name or f"{csv_path.stem} (Imported)"
    description = "Imported from NetEase playlist exporter"
    playlist_id = spotify_create_playlist(access_token, playlist_name, description, args.public)

    matched_uris = []
    unmatched = []
    cache: dict[tuple[str, str], str | None] = {}
    progress_path = csv_path.with_name(csv_path.stem + "_import_progress.json")
    progress_items = load_progress(progress_path, len(tracks))

    restored = sum(1 for x in progress_items if x is not None)
    if restored > 0:
        print(f"Resuming from saved progress: {restored}/{len(tracks)} already processed")

    for idx, t in enumerate(tracks, start=1):
        existing = progress_items[idx - 1]
        if existing is not None:
            if existing:
                matched_uris.append(existing)
            else:
                unmatched.append(t)
            continue

        key = (t["title"], t["artists"])
        if key in cache:
            uri = cache[key]
        else:
            uri = spotify_search_track(access_token, t["title"], t["artists"])
            cache[key] = uri
            # Small pacing to reduce rate-limit risk on long playlists.
            if args.throttle_ms > 0:
                time.sleep(args.throttle_ms / 1000.0)
        if uri:
            matched_uris.append(uri)
            progress_items[idx - 1] = uri
        else:
            unmatched.append(t)
            progress_items[idx - 1] = ""

        if idx % 50 == 0:
            print(f"Processed {idx}/{len(tracks)}")
            save_progress(progress_path, progress_items)

    if matched_uris:
        spotify_add_items(access_token, playlist_id, matched_uris)

    unmatched_path = csv_path.with_name(csv_path.stem + "_unmatched.csv")
    write_unmatched(unmatched, unmatched_path)
    save_progress(progress_path, progress_items)

    print("Done")
    print(f"Spotify playlist id: {playlist_id}")
    print(f"Matched: {len(matched_uris)}")
    print(f"Unmatched: {len(unmatched)}")
    print(f"Unmatched file: {unmatched_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
