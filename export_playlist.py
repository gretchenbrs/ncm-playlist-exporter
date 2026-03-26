#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

from ncm_exporter import export_playlist, get_playlist_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export public NetEase playlists to CSV/JSON"
    )
    parser.add_argument("--url", help="NetEase playlist URL")
    parser.add_argument("--id", dest="playlist_id", help="NetEase playlist id")
    parser.add_argument("--out", default="exports", help="Output directory")
    args = parser.parse_args()

    playlist_id = get_playlist_id(args.url, args.playlist_id)
    if not playlist_id:
        print("Missing playlist id. Use --id or --url.", file=sys.stderr)
        return 1

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        result = export_playlist(playlist_id, out_dir)
    except Exception as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1

    print(f"Export done: {result['playlist']['trackCount']} tracks")
    print(f"CSV : {result['csv_path']}")
    print(f"JSON: {result['json_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
