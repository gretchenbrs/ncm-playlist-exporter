# NetEase Playlist Exporter

One local website for:

- Export public NetEase playlists (CSV/JSON)
- Directly import NetEase playlists to Spotify (without manual CSV step)
- Manage local exported files

## Setup


Open:

- http://127.0.0.1:8765

## Spotify app setting

In Spotify Developer Dashboard, add redirect URI exactly:

- `http://127.0.0.1:8765/api/spotify/callback`

## Web flow

1. Paste NetEase playlist URL/ID in "直接导入 Spotify"
2. Click "连接 Spotify 并导入"
3. Complete Spotify auth in new tab
4. Watch live progress in the page
5. Open created playlist and download unmatched tracks if needed

## Optional file retention

Default local export retention is 72 hours.

```bash
export EXPORT_RETENTION_HOURS=72
python3 web_server.py
```

Set `EXPORT_RETENTION_HOURS=0` to disable cleanup.
