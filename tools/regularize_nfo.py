"""Preview current-format NFO regeneration, then apply the reviewed plan offline."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'app'))
from nfo_backfill import plan_current_nfo, apply_reviewed_entry  # noqa: E402
from nfo_metadata import IMVDbProvider  # noqa: E402
from nfo_musicbrainz import MusicBrainzSongs, PreferredGenres  # noqa: E402
from nfo_genres import SongGenres  # noqa: E402
from nfo_wikipedia import SongDescription  # noqa: E402


def enabled(name):
    return os.getenv(name, 'true').lower() in ('true', '1', 'on')


def providers(cache):
    envfile = SOURCE / '.env'
    if envfile.is_file():
        for line in envfile.read_text(encoding='utf-8-sig').splitlines():
            name, separator, value = line.partition('=')
            if separator and name in ('IMVDB_APP_KEY', 'IMVDB_SEARCH_ENABLED', 'IMVDB_WEB_SEARCH_ENABLED'):
                os.environ.setdefault(name, value.strip().strip('"').strip("'"))
    key = os.getenv('IMVDB_APP_KEY', '')
    if not key.strip():
        raise ValueError('Set IMVDB_APP_KEY before generating a library preview')
    mb = MusicBrainzSongs(cache / 'musicbrainz-songs')
    return (IMVDbProvider(key, cache / 'imvdb-cache',
                         search_enabled=enabled('IMVDB_SEARCH_ENABLED') if 'IMVDB_SEARCH_ENABLED' in os.environ else False,
                         web_search_enabled=enabled('IMVDB_WEB_SEARCH_ENABLED')),
            PreferredGenres(mb, SongGenres(cache / 'song-genres'), enabled=enabled('MUSICBRAINZ_ENABLED')) if enabled('NFO_GENRES_ENABLED') else None,
            SongDescription(mb, cache / 'wikipedia-descriptions', os.getenv('NFO_WIKIPEDIA_LANGUAGE', 'es'))
            if enabled('MUSICBRAINZ_ENABLED') and enabled('NFO_WIKIPEDIA_ENABLED') else None)


def write_report(path, result):
    temporary = path.with_name(path.name + '.tmp')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, help='Existing library root; never moves or downloads media')
    parser.add_argument('--artist', action='append', help='Only these relative artist folders; repeatable')
    parser.add_argument('--limit', type=int, default=20, help='Maximum preview files; 0 means all (default 20)')
    parser.add_argument('--mode', choices=('refresh', 'missing'), default='refresh', help='Refresh schema or preserve existing values')
    parser.add_argument('--cache-dir', type=Path, help='Provider cache outside the library')
    parser.add_argument('--report', type=Path, required=True, help='JSON preview/apply report outside library')
    parser.add_argument('--apply-plan', type=Path, help='Apply exact successful proposals from a reviewed report; no network')
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error('--limit cannot be negative')
    root = args.root.resolve(strict=True)
    if not root.is_dir():
        parser.error('root must be a directory')
    report_path = args.report.resolve()
    if report_path.is_relative_to(root):
        parser.error('Store reports outside the library')
    rows = []
    if args.apply_plan:
        if args.apply_plan.resolve() == report_path:
            parser.error('Apply report must differ from the reviewed preview')
        plan = json.loads(args.apply_plan.read_text(encoding='utf-8-sig'))
        if plan.get('schema') != 1 or plan.get('operation') != 'preview':
            parser.error('Unsupported preview schema')
        # No provider construction or credentials are needed on this path.
        result = {'schema': 1, 'operation': 'apply', 'root': str(root), 'results': rows}
        write_report(report_path, result)
        for entry in plan['results']:
            if entry.get('status') != 'proposed':
                rows.append({'file': entry.get('relative_video'), 'status': 'skipped', 'reason': entry.get('status')})
            else:
                try:
                    rows.append(apply_reviewed_entry(entry, root))
                except Exception as error:
                    rows.append({'file': entry.get('relative_video'), 'status': 'error', 'error': type(error).__name__})
            write_report(report_path, result)
    else:
        cache = (args.cache_dir or Path(os.getenv('STATE_DIR') or str(report_path.parent)) / 'regularize-nfo-cache').resolve()
        if cache.is_relative_to(root):
            parser.error('Store provider caches outside the library')
        folders = [root / artist for artist in args.artist] if args.artist else [root]
        if any(not folder.resolve(strict=True).is_relative_to(root) for folder in folders):
            parser.error('Artist folders must be inside the library')
        provider, genres, description = providers(cache)
        result = {'schema': 1, 'operation': 'preview', 'mode': args.mode, 'root': str(root), 'results': rows}
        write_report(report_path, result)
        visited = set()
        for folder in folders:
            for video in sorted(folder.rglob('*')):
                if (not video.is_file() or video.suffix.lower() not in ('.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v')
                        or any(part.startswith(('_', '.')) for part in video.relative_to(root).parts)):
                    continue
                if (video.resolve() in visited or not video.resolve().is_relative_to(root)
                        or not video.with_suffix('.nfo').resolve().is_relative_to(root)):
                    continue
                visited.add(video.resolve())
                try:
                    _, _, row = plan_current_nfo(video, provider, genres=genres, description=description,
                                                 mode=args.mode, library_root=root)
                except Exception as error:
                    row = {'file': str(video), 'status': 'error', 'error': type(error).__name__}
                row['relative_video'] = video.relative_to(root).as_posix()
                rows.append(row)
                print(json.dumps({k: row.get(k) for k in ('relative_video', 'status', 'wikipedia_url', 'errors')}, ensure_ascii=True), flush=True)
                write_report(report_path, result)
                if args.limit and len(rows) >= args.limit:
                    break
            if args.limit and len(rows) >= args.limit:
                break
    summary = dict(Counter(row['status'] for row in rows))
    result['summary'] = summary
    write_report(report_path, result)
    print(json.dumps({'report': str(report_path), 'processed': len(rows), 'summary': summary}, ensure_ascii=True))
    return 1 if any(row['status'] in ('error', 'provider_error') for row in rows) else 0


if __name__ == '__main__':
    raise SystemExit(main())
