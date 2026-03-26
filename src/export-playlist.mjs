import fs from 'node:fs/promises';
import path from 'node:path';

const HELP = `
Usage:
  npm run export -- --url "https://music.163.com/#/playlist?id=123456"
  npm run export -- --id 123456 --out ./exports

Options:
  --url    NetEase playlist URL
  --id     NetEase playlist id
  --out    Output directory (default: ./exports)
`;

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (!key.startsWith('--')) continue;
    if (!value || value.startsWith('--')) {
      args[key.slice(2)] = true;
      continue;
    }
    args[key.slice(2)] = value;
    i += 1;
  }
  return args;
}

function getPlaylistId(inputUrl, id) {
  if (id) return String(id).trim();
  if (!inputUrl) return '';

  try {
    const direct = inputUrl.match(/(?:id=)(\d{5,})/);
    if (direct) return direct[1];
    const url = new URL(inputUrl);
    const fromSearch = url.searchParams.get('id');
    if (fromSearch) return fromSearch.trim();
    return '';
  } catch {
    const match = inputUrl.match(/(?:id=)(\d{5,})/);
    return match ? match[1] : '';
  }
}

function toCsvRow(cols) {
  return cols
    .map((v) => `"${String(v ?? '').replaceAll('"', '""')}"`)
    .join(',');
}

async function fetchPlaylist(id) {
  const endpoint = `https://music.163.com/api/v6/playlist/detail?id=${encodeURIComponent(id)}&n=10000&s=0`;

  const res = await fetch(endpoint, {
    headers: {
      'User-Agent':
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
      Referer: 'https://music.163.com/',
      Origin: 'https://music.163.com',
      Accept: 'application/json, text/plain, */*'
    }
  });

  if (!res.ok) {
    throw new Error(`HTTP ${res.status} ${res.statusText}`);
  }

  const data = await res.json();

  if (!data || data.code !== 200 || !data.playlist) {
    const code = data?.code ?? 'unknown';
    throw new Error(`NetEase API error (code=${code}). Playlist may be private or unavailable.`);
  }

  return data.playlist;
}

async function main() {
  const args = parseArgs(process.argv);

  if (args.help || args.h) {
    console.log(HELP.trim());
    return;
  }

  const playlistId = getPlaylistId(args.url, args.id);
  if (!playlistId) {
    console.error('Missing playlist id. Use --id or --url.');
    console.error(HELP.trim());
    process.exitCode = 1;
    return;
  }

  const outDir = path.resolve(process.cwd(), args.out ? String(args.out) : 'exports');
  await fs.mkdir(outDir, { recursive: true });

  const playlist = await fetchPlaylist(playlistId);
  const tracks = Array.isArray(playlist.tracks) ? playlist.tracks : [];

  const normalized = tracks.map((track, index) => ({
    index: index + 1,
    songId: track.id,
    title: track.name || '',
    artists: Array.isArray(track.ar) ? track.ar.map((a) => a.name).filter(Boolean).join(', ') : '',
    album: track.al?.name || '',
    durationMs: track.dt || 0
  }));

  const safeName = `${playlist.name || `playlist_${playlistId}`}`.replaceAll(/[\\/:*?"<>|]/g, '_').slice(0, 80);
  const base = `${safeName}_${playlistId}`;

  const csvHeader = toCsvRow(['index', 'songId', 'title', 'artists', 'album', 'durationMs']);
  const csvBody = normalized.map((row) => toCsvRow([row.index, row.songId, row.title, row.artists, row.album, row.durationMs]));
  const csv = [csvHeader, ...csvBody].join('\n');

  const csvPath = path.join(outDir, `${base}.csv`);
  const jsonPath = path.join(outDir, `${base}.json`);

  await fs.writeFile(csvPath, csv, 'utf8');
  await fs.writeFile(
    jsonPath,
    JSON.stringify(
      {
        playlist: {
          id: playlist.id,
          name: playlist.name,
          trackCount: normalized.length,
          creator: playlist.creator?.nickname || ''
        },
        tracks: normalized
      },
      null,
      2
    ),
    'utf8'
  );

  console.log(`Export done: ${normalized.length} tracks`);
  console.log(`CSV : ${csvPath}`);
  console.log(`JSON: ${jsonPath}`);
}

main().catch((err) => {
  console.error('Export failed:', err.message);
  process.exitCode = 1;
});
