import csv
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path


def get_playlist_id(url: str | None, playlist_id: str | None) -> str:
    if playlist_id:
        return playlist_id.strip()
    if not url:
        return ""

    m = re.search(r"id=(\d{5,})", url)
    if m:
        return m.group(1)

    try:
        parsed = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(parsed.query)
        value = q.get("id", [""])[0]
        if value.strip():
            return value.strip()

        # Support short URLs such as https://163cn.tv/xxxxx by following redirects.
        if "163cn.tv" in (parsed.netloc or ""):
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                    )
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                final_url = resp.geturl()
            m2 = re.search(r"id=(\d{5,})", final_url)
            if m2:
                return m2.group(1)
        return ""
    except Exception:
        return ""


def fetch_playlist(playlist_id: str) -> dict:
    endpoint = (
        "https://music.163.com/api/v6/playlist/detail"
        f"?id={urllib.parse.quote(playlist_id)}&n=10000&s=0"
    )

    req = urllib.request.Request(
        endpoint,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Referer": "https://music.163.com/",
            "Origin": "https://music.163.com",
            "Accept": "application/json, text/plain, */*",
        },
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        data = json.loads(resp.read().decode("utf-8", errors="replace"))

    if data.get("code") != 200 or "playlist" not in data:
        code = data.get("code", "unknown")
        raise RuntimeError(
            f"NetEase API error (code={code}). Playlist may be private or unavailable."
        )

    return data["playlist"]


def fetch_song_details(track_ids: list[str], chunk_size: int = 500) -> list[dict]:
    songs: list[dict] = []
    endpoint = "https://music.163.com/api/v3/song/detail"

    for i in range(0, len(track_ids), chunk_size):
        chunk = track_ids[i : i + chunk_size]
        payload = {
            "c": json.dumps([{"id": int(song_id)} for song_id in chunk], ensure_ascii=False),
            "ids": json.dumps([int(song_id) for song_id in chunk], ensure_ascii=False),
        }
        body = urllib.parse.urlencode(payload).encode("utf-8")

        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Referer": "https://music.163.com/",
                "Origin": "https://music.163.com",
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Song detail HTTP {resp.status}")
            data = json.loads(resp.read().decode("utf-8", errors="replace"))

        chunk_songs = data.get("songs") or []
        songs.extend(chunk_songs)

    return songs


def normalize_tracks(playlist: dict) -> list[dict]:
    tracks = playlist.get("tracks", []) or []
    normalized = []
    for i, track in enumerate(tracks, start=1):
        artists = ", ".join(
            [a.get("name", "") for a in (track.get("ar") or []) if a.get("name")]
        )
        normalized.append(
            {
                "index": i,
                "songId": track.get("id", ""),
                "title": track.get("name", ""),
                "artists": artists,
                "album": (track.get("al") or {}).get("name", ""),
                "durationMs": track.get("dt", 0),
            }
        )
    return normalized


def safe_name(name: str, playlist_id: str) -> str:
    base = name.strip() or f"playlist_{playlist_id}"
    base = re.sub(r"[\\/:*?\"<>|]", "_", base)
    return base[:80]


def export_playlist(playlist_id: str, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = get_playlist_payload(playlist_id)
    playlist = payload["playlist"]
    tracks = payload["tracks"]

    base = f"{safe_name(playlist.get('name', ''), playlist_id)}_{playlist_id}"
    csv_path = out_dir / f"{base}.csv"
    json_path = out_dir / f"{base}.json"

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["index", "songId", "title", "artists", "album", "durationMs"],
        )
        writer.writeheader()
        writer.writerows(tracks)

    payload = {
        "playlist": {
            "id": playlist.get("id"),
            "name": playlist.get("name", ""),
            "trackCount": len(tracks),
            "creator": (playlist.get("creator") or {}).get("nickname", ""),
        },
        "tracks": tracks,
    }

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    return {
        "playlist": payload["playlist"],
        "csv_path": str(csv_path),
        "json_path": str(json_path),
        "base_name": base,
    }


def get_playlist_payload(playlist_id: str) -> dict:
    playlist = fetch_playlist(playlist_id)
    raw_tracks = playlist.get("tracks", []) or []
    track_ids = [str(t.get("id")) for t in (playlist.get("trackIds") or []) if t.get("id")]

    # Some playlists only return a preview in `tracks` (often 10 songs).
    # In that case fetch full details by `trackIds`.
    if track_ids and len(raw_tracks) < len(track_ids):
        detailed_tracks = fetch_song_details(track_ids)
        by_id = {str(song.get("id")): song for song in detailed_tracks if song.get("id")}
        ordered_tracks = [by_id[sid] for sid in track_ids if sid in by_id]
        tracks = normalize_tracks({"tracks": ordered_tracks})
    else:
        tracks = normalize_tracks({"tracks": raw_tracks})

    payload = {
        "playlist": {
            "id": playlist.get("id"),
            "name": playlist.get("name", ""),
            "trackCount": len(tracks),
            "creator": (playlist.get("creator") or {}).get("nickname", ""),
        },
        "tracks": tracks,
    }
    return payload
