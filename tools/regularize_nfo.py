"""Review existing music videos; dry-run by default, --apply explicitly writes NFO."""
import argparse
import json
import os
from pathlib import Path
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'app'))
from nfo_backfill import plan_nfo, apply_nfo  # noqa: E402
from nfo_metadata import IMVDbProvider  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, help='Existing library root')
    parser.add_argument('--artist', action='append', help='Limit to these artist folders (repeatable)')
    parser.add_argument('--limit', type=int, default=20, help='Maximum files; default 20')
    parser.add_argument('--report', type=Path, help='Write a JSON report to this explicit path')
    parser.add_argument('--apply', action='store_true', help='Write after backing up existing NFOs')
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('--limit must be positive')
    root = args.root.resolve(strict=True)
    env_file = SOURCE / '.env'
    if env_file.exists():
        for line in env_file.read_text(encoding='utf-8-sig').splitlines():
            name, separator, value = line.partition('=')
            if separator and name in ('IMVDB_APP_KEY', 'IMVDB_SEARCH_ENABLED', 'IMVDB_WEB_SEARCH_ENABLED'):
                os.environ.setdefault(name, value.strip())
    key = os.getenv('IMVDB_APP_KEY', '')
    if not key:
        parser.error('Set IMVDB_APP_KEY in the environment or private project .env')
    provider = IMVDbProvider(key, search_enabled=os.getenv('IMVDB_SEARCH_ENABLED', 'false').lower() == 'true',
                             web_search_enabled=os.getenv('IMVDB_WEB_SEARCH_ENABLED', 'true').lower() == 'true')
    folders = [root / artist for artist in args.artist] if args.artist else [root]
    reports = []
    for folder in folders:
        if not folder.resolve().is_relative_to(root):
            parser.error('Artist folder must be within the library root')
        for video in sorted(folder.rglob('*')):
            if not video.is_file() or video.suffix.lower() not in ('.mp4', '.mkv', '.webm', '.mov', '.avi'):
                continue
            if not video.resolve().is_relative_to(root) or not video.with_suffix('.nfo').resolve().is_relative_to(root):
                continue
            try:
                target, payload, report = plan_nfo(video, provider)
                if payload and args.apply:
                    backup = apply_nfo(target, payload, root)
                    report.update(status='applied', backup=str(backup) if backup else None)
                elif payload:
                    report['status'] = 'dry_run'
                reports.append(report)
            except Exception as exc:
                reports.append({'file': str(video), 'status': 'error', 'error': type(exc).__name__})
            if len(reports) >= args.limit:
                break
        if len(reports) >= args.limit:
            break
    result = {'apply': args.apply, 'processed': len(reports), 'results': reports}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        if args.report.resolve().is_relative_to(root):
            parser.error('Store the report outside the library to keep dry-run read-only')
        args.report.write_text(text, encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
