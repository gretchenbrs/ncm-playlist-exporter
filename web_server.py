#!/usr/bin/env python3
import csv
import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ncm_exporter import export_playlist, get_playlist_id, get_playlist_payload

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
EXPORT_DIR = ROOT / "exports"
HOST = "127.0.0.1"
PORT = int(os.getenv("PORT", "8765"))
RETENTION_HOURS = int(os.getenv("EXPORT_RETENTION_HOURS", "72"))

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", f"http://{HOST}:{PORT}/api/spotify/callback").strip()
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SPOTIFY_SCOPE = "playlist-modify-private playlist-modify-public"

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def cleanup_exports() -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    if RETENTION_HOURS <= 0:
        return

    now = time.time()
    ttl_seconds = RETENTION_HOURS * 3600
    for p in EXPORT_DIR.glob("*"):
        if not p.is_file():
            continue
        age = now - p.stat().st_mtime
        if age > ttl_seconds:
            try:
                p.unlink()
            except OSError:
                pass


def list_export_files() -> list[dict]:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    for p in EXPORT_DIR.glob("*"):
        if not p.is_file() or p.suffix.lower() not in {".csv", ".json"}:
            continue
        st = p.stat()
        records.append(
            {
                "name": p.name,
                "size": st.st_size,
                "modifiedAt": int(st.st_mtime),
                "downloadUrl": f"/api/download?file={p.name}",
            }
        )

    records.sort(key=lambda r: r["modifiedAt"], reverse=True)
    return records


def _validate_export_path(name: str) -> Path | None:
    if not name:
        return None
    candidate = (EXPORT_DIR / name).resolve()
    try:
        candidate.relative_to(EXPORT_DIR.resolve())
    except ValueError:
        return None
    return candidate


def _spotify_auth_header() -> str:
    import base64

    raw = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("utf-8")


def _spotify_http_post_form(url: str, data: dict, headers: dict | None = None, retries: int = 6) -> dict:
    payload = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < retries - 1:
                retry_after = e.headers.get("Retry-After")
                wait_s = float(retry_after) if retry_after else (1.5 * (attempt + 1))
                if wait_s > 300:
                    raise RuntimeError(f"Spotify rate-limit window too long ({wait_s:.0f}s). Please retry later.") from e
                wait_s += random.uniform(0.0, 0.4)
                time.sleep(wait_s)
                continue
            raise RuntimeError(f"HTTP {e.code} on POST {url}: {detail}") from e


def _spotify_http_get(url: str, headers: dict | None = None, retries: int = 6) -> dict:
    req = urllib.request.Request(url, method="GET")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < retries - 1:
                retry_after = e.headers.get("Retry-After")
                wait_s = float(retry_after) if retry_after else (1.5 * (attempt + 1))
                if wait_s > 300:
                    raise RuntimeError(f"Spotify rate-limit window too long ({wait_s:.0f}s). Please retry later.") from e
                wait_s += random.uniform(0.0, 0.4)
                time.sleep(wait_s)
                continue
            raise RuntimeError(f"HTTP {e.code} on GET {url}: {detail}") from e


def _spotify_http_post_json(url: str, body: dict, headers: dict, retries: int = 6) -> dict:
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < retries - 1:
                retry_after = e.headers.get("Retry-After")
                wait_s = float(retry_after) if retry_after else (1.5 * (attempt + 1))
                if wait_s > 300:
                    raise RuntimeError(f"Spotify rate-limit window too long ({wait_s:.0f}s). Please retry later.") from e
                wait_s += random.uniform(0.0, 0.4)
                time.sleep(wait_s)
                continue
            raise RuntimeError(f"HTTP {e.code} on POST {url}: {detail}") from e


def _spotify_exchange_code(code: str) -> str:
    data = _spotify_http_post_form(
        SPOTIFY_TOKEN_URL,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": SPOTIFY_REDIRECT_URI},
        headers={"Authorization": _spotify_auth_header()},
    )
    access = data.get("access_token")
    if not access:
        raise RuntimeError(f"Spotify token exchange failed: {data}")
    return access


def _spotify_me(access_token: str) -> dict:
    return _spotify_http_get(
        f"{SPOTIFY_API_BASE}/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )


def _spotify_create_playlist(access_token: str, name: str, is_public: bool) -> str:
    data = _spotify_http_post_json(
        f"{SPOTIFY_API_BASE}/me/playlists",
        {"name": name, "description": "Imported from NetEase playlist tool", "public": is_public},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    playlist_id = data.get("id")
    if not playlist_id:
        raise RuntimeError(f"Spotify create playlist failed: {data}")
    return str(playlist_id)


def _spotify_search_track(access_token: str, title: str, artists: str) -> str | None:
    q = f'track:"{title}"'
    first_artist = artists.split(",")[0].strip() if artists else ""
    if first_artist:
        q += f' artist:"{first_artist}"'

    params = urllib.parse.urlencode({"q": q, "type": "track", "limit": 5})
    data = _spotify_http_get(
        f"{SPOTIFY_API_BASE}/search?{params}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    items = ((data.get("tracks") or {}).get("items") or [])
    if not items:
        return None
    return items[0].get("uri")


def _spotify_add_items(access_token: str, playlist_id: str, uris: list[str]) -> None:
    for i in range(0, len(uris), 100):
        chunk = uris[i : i + 100]
        _spotify_http_post_json(
            f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/tracks",
            {"uris": chunk},
            headers={"Authorization": f"Bearer {access_token}"},
        )


def _job_public(job: dict) -> dict:
    return {
        "id": job["id"],
        "status": job["status"],
        "message": job.get("message", ""),
        "progress": {
            "processed": job.get("processed", 0),
            "total": job.get("total", 0),
            "matched": job.get("matched", 0),
            "unmatched": job.get("unmatched", 0),
        },
        "playlistName": job.get("spotify_playlist_name", ""),
        "spotifyPlaylistUrl": job.get("spotify_playlist_url"),
        "unmatchedDownloadUrl": f"/api/spotify/unmatched?jobId={job['id']}" if job.get("unmatched_path") else None,
    }


def _run_spotify_import(job_id: str, code: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["message"] = "正在导入 Spotify..."

    try:
        access_token = _spotify_exchange_code(code)
        me = _spotify_me(access_token)
        if not me.get("id"):
            raise RuntimeError("Spotify profile fetch failed")

        playlist_name = job.get("spotify_playlist_name") or f"{job['playlist']['name']} (Imported)"
        playlist_id = _spotify_create_playlist(access_token, playlist_name, bool(job.get("spotify_public")))

        matched_uris: list[str] = []
        unmatched: list[dict] = []
        cache: dict[tuple[str, str], str | None] = {}
        tracks = job["tracks"]
        throttle_ms = max(0, int(job.get("throttle_ms", 220)))

        for i, track in enumerate(tracks, start=1):
            key = (track["title"], track["artists"])
            if key in cache:
                uri = cache[key]
            else:
                uri = _spotify_search_track(access_token, track["title"], track["artists"])
                cache[key] = uri
                if throttle_ms > 0:
                    time.sleep(throttle_ms / 1000.0)

            if uri:
                matched_uris.append(uri)
            else:
                unmatched.append(track)

            if i % 10 == 0 or i == len(tracks):
                with JOBS_LOCK:
                    job["processed"] = i
                    job["matched"] = len(matched_uris)
                    job["unmatched"] = len(unmatched)

        if matched_uris:
            _spotify_add_items(access_token, playlist_id, matched_uris)

        unmatched_path = EXPORT_DIR / f"spotify_unmatched_{job_id}.csv"
        with unmatched_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["title", "artists", "album"])
            writer.writeheader()
            writer.writerows(unmatched)

        with JOBS_LOCK:
            job["status"] = "done"
            job["processed"] = len(tracks)
            job["matched"] = len(matched_uris)
            job["unmatched"] = len(unmatched)
            job["unmatched_path"] = str(unmatched_path)
            job["spotify_playlist_url"] = f"https://open.spotify.com/playlist/{playlist_id}"
            job["message"] = "导入完成"
    except Exception as exc:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                job["status"] = "error"
                job["message"] = str(exc)


class AppHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, file_path: Path, content_type: str):
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return

        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        cleanup_exports()
        parsed = urlparse(self.path)

        if parsed.path == "/":
            return self._serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")

        if parsed.path == "/api/files":
            return self._send_json(
                HTTPStatus.OK,
                {
                    "files": list_export_files(),
                    "retentionHours": RETENTION_HOURS,
                },
            )

        if parsed.path == "/api/download":
            params = parse_qs(parsed.query)
            name = (params.get("file") or [""])[0]
            candidate = _validate_export_path(name)
            if not candidate:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid file"})

            ctype = "application/octet-stream"
            if candidate.suffix.lower() == ".csv":
                ctype = "text/csv; charset=utf-8"
            elif candidate.suffix.lower() == ".json":
                ctype = "application/json; charset=utf-8"

            return self._serve_file(candidate, ctype)

        if parsed.path == "/api/spotify/jobs":
            params = parse_qs(parsed.query)
            job_id = (params.get("jobId") or [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Job not found"})
            return self._send_json(HTTPStatus.OK, _job_public(job))

        if parsed.path == "/api/spotify/unmatched":
            params = parse_qs(parsed.query)
            job_id = (params.get("jobId") or [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Job not found"})
            unmatched_path = job.get("unmatched_path")
            if not unmatched_path:
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": "No unmatched file"})
            p = Path(unmatched_path)
            return self._serve_file(p, "text/csv; charset=utf-8")

        if parsed.path == "/api/spotify/callback":
            params = parse_qs(parsed.query)
            state = (params.get("state") or [""])[0]
            code = (params.get("code") or [""])[0]
            error = (params.get("error") or [""])[0]

            with JOBS_LOCK:
                job = JOBS.get(state)
            if not job:
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<h3>Invalid import session.</h3>".encode("utf-8"))
                return

            if error:
                with JOBS_LOCK:
                    job["status"] = "error"
                    job["message"] = f"Spotify auth failed: {error}"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<h3>授权失败，请回到原页面重试。</h3>".encode("utf-8"))
                return

            with JOBS_LOCK:
                if job["status"] in {"running", "done"}:
                    pass
                else:
                    job["status"] = "auth_received"
                    job["message"] = "授权成功，开始导入..."

            threading.Thread(target=_run_spotify_import, args=(state, code), daemon=True).start()

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h3>Spotify 授权成功，正在导入。你可以关闭这个页面并回到原窗口查看进度。</h3>".encode("utf-8"))
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_DELETE(self):
        cleanup_exports()
        parsed = urlparse(self.path)
        if parsed.path != "/api/files":
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return

        params = parse_qs(parsed.query)
        name = (params.get("file") or [""])[0]
        candidate = _validate_export_path(name)
        if not candidate:
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid file"})
        if not candidate.exists() or not candidate.is_file():
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "File not found"})

        try:
            candidate.unlink()
        except OSError as exc:
            return self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        return self._send_json(HTTPStatus.OK, {"ok": True})

    def do_POST(self):
        cleanup_exports()
        parsed = urlparse(self.path)

        if parsed.path == "/api/export":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
                source = str(payload.get("source", "")).strip()
            except Exception:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request body"})

            playlist_id = get_playlist_id(source, source if source.isdigit() else None)
            if not playlist_id:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "无法识别歌单ID，请检查链接"})

            try:
                result = export_playlist(playlist_id, EXPORT_DIR)
            except Exception as exc:
                return self._send_json(HTTPStatus.BAD_GATEWAY, {"error": f"导出失败: {exc}"})

            csv_name = Path(result["csv_path"]).name
            json_name = Path(result["json_path"]).name

            return self._send_json(
                HTTPStatus.OK,
                {
                    "playlist": result["playlist"],
                    "csvDownloadUrl": f"/api/download?file={csv_name}",
                    "jsonDownloadUrl": f"/api/download?file={json_name}",
                },
            )

        if parsed.path == "/api/spotify/import/start":
            if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
                return self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": "Missing SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in server env",
                    },
                )

            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
                source = str(payload.get("source", "")).strip()
                playlist_name = str(payload.get("playlistName", "")).strip()
                throttle_ms = int(payload.get("throttleMs", 220))
                is_public = bool(payload.get("public", False))
            except Exception:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request body"})

            playlist_id = get_playlist_id(source, source if source.isdigit() else None)
            if not playlist_id:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "无法识别歌单ID，请检查链接"})

            try:
                ncm_payload = get_playlist_payload(playlist_id)
            except Exception as exc:
                return self._send_json(HTTPStatus.BAD_GATEWAY, {"error": f"歌单读取失败: {exc}"})

            job_id = uuid.uuid4().hex
            with JOBS_LOCK:
                JOBS[job_id] = {
                    "id": job_id,
                    "status": "awaiting_auth",
                    "message": "等待 Spotify 授权",
                    "playlist": ncm_payload["playlist"],
                    "tracks": ncm_payload["tracks"],
                    "total": len(ncm_payload["tracks"]),
                    "processed": 0,
                    "matched": 0,
                    "unmatched": 0,
                    "spotify_playlist_name": playlist_name or f"{ncm_payload['playlist']['name']} (Imported)",
                    "spotify_public": is_public,
                    "throttle_ms": max(80, min(1500, throttle_ms)),
                    "created_at": int(time.time()),
                }

            query = urllib.parse.urlencode(
                {
                    "response_type": "code",
                    "client_id": SPOTIFY_CLIENT_ID,
                    "scope": SPOTIFY_SCOPE,
                    "redirect_uri": SPOTIFY_REDIRECT_URI,
                    "state": job_id,
                    "show_dialog": "true",
                }
            )
            auth_url = f"{SPOTIFY_AUTH_URL}?{query}"

            return self._send_json(
                HTTPStatus.OK,
                {
                    "jobId": job_id,
                    "authUrl": auth_url,
                    "playlist": ncm_payload["playlist"],
                },
            )

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")


def main():
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_exports()
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"Server running at http://{HOST}:{PORT}")
    print(f"Export retention hours: {RETENTION_HOURS}")
    print(f"Spotify redirect URI: {SPOTIFY_REDIRECT_URI}")
    server.serve_forever()


if __name__ == "__main__":
    main()
