"""Optional sidecars; the IMVDb V1 adapter is isolated for the announced V2 migration."""

import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
import unicodedata
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler
import xml.etree.ElementTree as ET
from datetime import date
from html.parser import HTMLParser

from yt_dlp.postprocessor.common import PostProcessor

log = logging.getLogger('ytdl')


def imvdb_video_path(value):
    """Only public video pages on the fixed IMVDb host, never arbitrary URLs."""
    try:
        parsed = urlsplit(str(value))
    except ValueError:
        return None
    if (parsed.scheme == 'https' and parsed.netloc == 'imvdb.com'
            and not parsed.query and not parsed.fragment
            and re.fullmatch(r'/video/[A-Za-z0-9%_-]+/[A-Za-z0-9%_-]+/?', parsed.path)):
        return parsed.path.rstrip('/')
    return None


class _PublicPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            href = dict(attrs).get('href', '')
            if href.startswith('/video/'):
                href = 'https://imvdb.com' + href
            path = imvdb_video_path(href)
            if path and path not in self.links:
                self.links.append(path)

    def handle_data(self, data):
        self.text.append(data)


def public_video_id(html):
    parser = _PublicPage()
    parser.feed(html)
    ids = set(re.findall(r'\bID:\s*(\d{1,20})\b', ' '.join(parser.text)))
    return next(iter(ids)) if len(ids) == 1 else None


def youtube_reference_ids(video):
    ids = []
    for source in video.get('sources') or []:
        if source.get('source') != 'youtube':
            continue
        value = str(source.get('source_data') or '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
            try:
                parsed = urlsplit(value)
            except ValueError:
                continue
            if parsed.hostname in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
                value = parse_qs(parsed.query).get('v', [''])[0]
            elif parsed.hostname == 'youtu.be':
                value = parsed.path.strip('/')
        if re.fullmatch(r'[A-Za-z0-9_-]{11}', value) and value not in ids:
            ids.append(value)
    return ids


def _normalized_name(value):
    value = unicodedata.normalize('NFKD', str(value)).casefold()
    return ''.join(c for c in value if c.isalnum() and not unicodedata.combining(c))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class IMVDbProvider:
    """Never accept a title-only match: require the exact YouTube source ID."""

    def __init__(self, key, cache_dir=None, *, search_enabled=False, web_search_enabled=True):
        self.key = key
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.search_enabled = search_enabled
        self.web_search_enabled = web_search_enabled
        self._deadline = None

    def _read(self, url, headers):
        timeout = 6
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('IMVDb lookup budget exhausted')
            timeout = min(timeout, remaining)
        request = Request(url, headers={'User-Agent': 'MeTube-NFO/1', **headers})
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError('IMVDb response too large')
        return raw

    def _get_html(self, path, **params):
        if path != '/search' and imvdb_video_path('https://imvdb.com' + path) is None:
            raise ValueError('Invalid public IMVDb path')
        url = 'https://imvdb.com' + path
        if params:
            url += '?' + urlencode(params)
        # The app key is sent only to the API, never to scraped pages.
        return self._read(url, {'Accept': 'text/html'}).decode('utf-8')

    def _get(self, endpoint, **params):
        url = 'https://imvdb.com/api/v1/' + endpoint + '?' + urlencode(params)
        raw = self._read(url, {
            'IMVDB-APP-KEY': self.key, 'Accept': 'application/json',
        })
        # No key-bearing redirects to other hosts, no site-controlled endpoints.
        return json.loads(raw)

    def _web_candidates(self, query):
        parser = _PublicPage()
        parser.feed(self._get_html('/search', search_term=query))
        for path in parser.links[:3]:
            video_id = public_video_id(self._get_html(path))
            if video_id:
                yield {'id': video_id}

    def find_by_artist_title(self, artist, title):
        """For a reviewed backfill: exact names, not a claimed source-file match."""
        if not self.key or not artist or not title:
            return None
        self._deadline = time.monotonic() + 15
        query = f'{artist} {title}'[:200]
        if self.search_enabled:
            candidates = self._get('search/videos', q=query, per_page=5).get('results') or []
        elif self.web_search_enabled:
            candidates = list(self._web_candidates(query))
        else:
            return None
        expected_artists = {_normalized_name(name) for name in re.split(r'[,;]', artist)}
        matches = []
        for item in candidates[:5]:
            video_id = str(item.get('id') or '')
            if not re.fullmatch(r'\d{1,20}', video_id):
                continue
            video = self._get('video/' + video_id, include='sources,credits')
            artists = {_normalized_name(a.get('name', '')) for a in video.get('artists') or []}
            if (_normalized_name(video.get('song_title', '')) == _normalized_name(title)
                    and artists & expected_artists):
                matches.append(video)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _matches(video, youtube_id):
        return youtube_id in youtube_reference_ids(video)

    def lookup(self, info, imvdb_id=''):
        self._deadline = time.monotonic() + 15
        youtube_id = str(info.get('id') or '')
        if not self.key or not str(info.get('extractor_key', '')).lower().startswith('youtube'):
            return None
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', youtube_id):
            return None
        cache = self.cache_dir / (youtube_id + '.json') if self.cache_dir else None
        if cache and cache.is_file() and time.time() - cache.stat().st_mtime < 86400:
            try:
                video = json.loads(cache.read_text(encoding='utf-8'))
                same_reference = (not imvdb_id or str(video.get('id')) == imvdb_id
                                  or (imvdb_video_path(imvdb_id) is not None
                                      and imvdb_video_path(video.get('url', '')) == imvdb_video_path(imvdb_id)))
                if self._matches(video, youtube_id) and same_reference:
                    return video
            except (OSError, ValueError, TypeError):
                pass
        if imvdb_id:
            path = imvdb_video_path(imvdb_id)
            if path:
                imvdb_id = public_video_id(self._get_html(path)) or ''
            if not re.fullmatch(r'\d{1,20}', imvdb_id):
                return None
            result = {'results': [{'id': imvdb_id}]}
        else:
            artist = info.get('artist') or ', '.join(info.get('artists') or [])
            query = (f"{artist} {info['track']}".strip() if info.get('track')
                     else str(info.get('title') or ''))[:200]
            if self.search_enabled:
                result = self._get('search/videos', q=query, per_page=5)
            elif self.web_search_enabled:
                # Remove only a conventional display suffix; no guessed artist parsing.
                query = re.sub(r'\s*[\[(]official (?:music )?video[\])]\s*$', '', query, flags=re.I)
                result = {'results': list(self._web_candidates(query))}
            else:
                return None
        matches = []
        for item in (result.get('results') or [])[:5]:
            video_id = str(item.get('id') or '')
            if not video_id.isdecimal():
                continue
            video = self._get('video/' + video_id, include='sources,credits')
            if self._matches(video, youtube_id):
                matches.append(video)
        if len(matches) != 1:
            return None
        video = matches[0]
        if cache:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                # Cache only the response; never the request headers or key.
                with NamedTemporaryFile(mode='w', encoding='utf-8', dir=cache.parent,
                                        delete=False) as stream:
                    json.dump(video, stream)
                    temporary = Path(stream.name)
                temporary.replace(cache)
            except OSError:
                log.warning('NFO: could not save IMVDb cache')
        return video


def make_nfo(info, video=None):
    """Music-video sidecar, with generic source fields when identification fails."""
    root = ET.Element('musicvideo')

    def add(name, value):
        if value is not None and str(value).strip():
            # XML 1.0 excludes control characters sometimes present in descriptions.
            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', '', str(value))
            ET.SubElement(root, name).text = text

    add('title', video.get('song_title') if video else info.get('title'))
    add('plot', info.get('description'))
    if not video:
        artists = info.get('artists') or ([info['artist']] if info.get('artist') else [])
        for artist in artists:
            add('artist', artist)
    if info.get('duration') is not None:
        add('runtime', max(1, round(float(info['duration']) / 60)))
    identifier = info.get('id')
    if identifier:
        element = ET.SubElement(root, 'uniqueid', type=str(info.get('extractor_key') or 'source').lower(), default='true')
        element.text = str(identifier)
    # Keep upload date explicitly labelled; never call it the original premiere.
    source_url = info.get('webpage_url')
    if not source_url and str(info.get('extractor_key', '')).lower() == 'youtube' and identifier:
        source_url = 'https://www.youtube.com/watch?v=' + str(identifier)
    for name, value in [('Source URL', source_url),
                        ('Source title', info.get('_metube_source_title')),
                        ('Channel', info.get('channel') or info.get('uploader')),
                        ('YouTube upload date', info.get('upload_date'))]:
        if value:
            add('tag', f'{name}: {value}')
    for tag in (info.get('tags') or [])[:50]:
        add('tag', tag)
    if video:
        year = str(video.get('year') or '')
        if re.fullmatch(r'\d{4}', year):
            add('year', year)
        # V1 has both year and a display date. Only accept a full, valid day.
        release = str(video.get('release_date_string') or '')
        match = re.fullmatch(r'([A-Za-z]+) (\d{1,2}), (\d{4})', release)
        months = ('January February March April May June July August September '
                  'October November December').split()
        if match and match[1] in months:
            try:
                add('premiered', date(int(match[3]), months.index(match[1]) + 1, int(match[2])).isoformat())
            except ValueError:
                pass
        for artist in video.get('artists') or []:
            add('artist', artist.get('name'))
        for director in video.get('directors') or []:
            add('director', director.get('entity_name'))
        add('tag', 'Metadata source: IMVDb ' + str(video.get('id', '')))
        if video.get('id'):
            ET.SubElement(root, 'uniqueid', type='imvdb', default='false').text = str(video['id'])
        if imvdb_video_path(video.get('url', '')):
            add('tag', 'IMVDb page: ' + video['url'])
        for source_id in youtube_reference_ids(video):
            add('tag', 'YouTube reference from IMVDb: https://www.youtube.com/watch?v=' + source_id)
    else:
        add('tag', 'Metadata source: original video (generic fallback)')
    ET.indent(root)
    return root


class NFOMetadataPreProcessor(PostProcessor):
    """Resolve credits before filename expansion; folders are created by yt-dlp."""

    def __init__(self, downloader, *, provider, imvdb_id='', artist_folders=False,
                 directory=None, sanitize=None, within=None, allowed=None):
        super().__init__(downloader)
        self.provider = provider
        self.imvdb_id = imvdb_id
        self.artist_folders = artist_folders
        self.directory = Path(directory).resolve() if directory else None
        self.sanitize = sanitize
        self.within = within
        self.allowed = allowed

    def run(self, info):
        video = None
        try:
            video = self.provider.lookup(info, self.imvdb_id)
        except Exception:
            self.report_warning('IMVDb unavailable; using original video metadata')
        info['_metube_imvdb_checked'] = True
        if video:
            info['_metube_imvdb'] = video
        # A shared artist directory must not turn "Artist - Song" filenames
        # into Jellyfin's movie-version naming pattern. Prefer a verified song
        # title or the extractor's structured track, never guessed title splits.
        title = video.get('song_title') if video else info.get('track')
        if isinstance(title, str) and title.strip():
            info.setdefault('_metube_source_title', info.get('title'))
            info['title'] = title.strip()
        if self.artist_folders:
            artists = (video.get('artists') or []) if video else []
            artist = artists[0].get('name') if artists else None
            if not artist:
                original = info.get('artists') or []
                artist = original[0] if original else info.get('artist')
            # Preserve a useful default even if directory enumeration fails.
            name = self.sanitize(str(artist or 'Sin artista'))
            if self.allowed and not self.allowed((self.directory / name).resolve()):
                name = self.sanitize(name + ' - artist')
            info['metube_artist'] = name
            try:
                candidates = []
                for path in self.directory.iterdir():
                    if (path.name.startswith('.') or not path.is_dir()
                            or not self.within(str(self.directory), str(path.resolve()))
                            or (self.allowed and not self.allowed(path.resolve()))):
                        continue
                    if _normalized_name(path.name) == _normalized_name(name):
                        candidates.append(path.name)
                if name in candidates:
                    info['metube_artist'] = name
                elif len(candidates) == 1:
                    # Reuse a-ha/A-ha or AC-DC/AC_DC rather than creating duplicates.
                    info['metube_artist'] = candidates[0]
            except OSError:
                self.report_warning('Could not inspect artist folders; using sanitized artist name')
        return [], info


class NFOPostProcessor(PostProcessor):
    def __init__(self, downloader, *, roots, profile, poster, within, state_dir=None, imvdb_id=''):
        super().__init__(downloader)
        self.roots = roots
        self.profile = profile
        self.poster = poster
        self.imvdb_id = imvdb_id
        self.within = within
        self.provider = IMVDbProvider(
            os.getenv('IMVDB_APP_KEY', ''),
            Path(state_dir) / 'imvdb-cache' if state_dir else None,
            search_enabled=os.getenv('IMVDB_SEARCH_ENABLED', 'false').lower() in ('true', '1', 'on'),
            web_search_enabled=os.getenv('IMVDB_WEB_SEARCH_ENABLED', 'true').lower() in ('true', '1', 'on'))
        self.state_dir = Path(state_dir).resolve() if state_dir else None

    def _safe(self, path):
        if (self.state_dir and self.within(str(self.state_dir), str(path))
                and not any(self.within(str(self.state_dir), str(Path(root).resolve())) for root in self.roots)):
            return False
        return any(self.within(str(Path(root).resolve()), str(path)) for root in self.roots)

    def run(self, info):
        try:
            path = Path(info['filepath']).resolve()
            target = path.with_suffix('.nfo')
            if not path.is_file() or not self._safe(path) or not self._safe(target):
                raise ValueError('NFO path outside download directory or missing video')
            if target.exists():
                self.report_warning('NFO already exists; preserving it')
                return [], info
            video = None
            try:
                video = (info.get('_metube_imvdb') if info.get('_metube_imvdb_checked')
                         else self.provider.lookup(info, self.imvdb_id))
            except Exception:
                # Do not log HTTP requests or credentials. Supplemental only.
                self.report_warning('IMVDb unavailable; writing generic NFO')
            if not video:
                self.report_warning('No verified IMVDb match; writing generic NFO')
            root = make_nfo(info, video)
            if self.poster:
                try:
                    for thumb in reversed(info.get('thumbnails') or []):
                        source = Path(thumb.get('filepath') or '')
                        if source.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
                            continue
                        # yt-dlp moves sidecars after conversion without updating
                        # thumbnail.filepath; resolve the final move map as well.
                        moves = info.get('__files_to_move') or {}
                        old = str(source)
                        if old in moves:
                            source = Path(moves[old] or (path.parent / source.name))
                        elif not source.is_file():
                            # after_move no longer exposes __files_to_move.
                            source = path.parent / source.name
                        source = source.resolve()
                        cover = path.with_name(path.stem + '-poster' + source.suffix.lower())
                        if source.is_file() and self._safe(source) and self._safe(cover):
                            # Exclusive creation preserves user-provided artwork.
                            if not cover.exists():
                                with source.open('rb') as src, cover.open('xb') as dst:
                                    shutil.copyfileobj(src, dst)
                            ET.SubElement(root, 'thumb').text = cover.name
                            break
                    else:
                        self.report_warning('No local thumbnail available for NFO')
                except Exception:
                    self.report_warning('Could not save NFO poster; writing metadata only')
            # Publish a complete file atomically; a failed write cannot leave a
            # truncated NFO, and link() refuses to replace an existing sidecar.
            payload = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            temporary = None
            try:
                with NamedTemporaryFile(dir=target.parent, prefix='.nfo-', suffix='.tmp', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(payload)
                os.chmod(temporary, path.stat().st_mode & 0o666)
                os.link(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        except Exception:
            self.report_warning('Could not write NFO; keeping downloaded video')
        return [], info
