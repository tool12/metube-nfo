"""Merge optional library metadata while preserving existing NFO values."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import os
import errno
import stat
import hashlib
import json
import re
import shutil
from tempfile import NamedTemporaryFile
import uuid
import xml.etree.ElementTree as ET

from nfo_metadata import make_nfo, youtube_reference_ids, _normalized_name, _failure_code


def read_nfo(path):
    if not path.exists():
        return None
    if path.stat().st_size > 2_000_000:
        raise ValueError('Existing NFO exceeds size limit')
    return ET.fromstring(path.read_bytes())


def merge_missing(existing, proposed):
    """Preserve user values and unknown fields; add missing fields and references."""
    result = deepcopy(existing) if existing is not None else ET.Element(proposed.tag)
    original_values = {n.tag for n in result if n.text and n.text.strip()}
    for node in proposed:
        if node.tag == 'actor':
            name = (node.findtext('name') or '').strip().casefold()
            if name and not any((n.findtext('name') or '').strip().casefold() == name for n in result.findall('actor')):
                result.append(deepcopy(node))
            continue
        if node.tag == 'metube':
            if result.find('metube') is None:
                result.append(deepcopy(node))
            continue
        if node.tag == 'tag':
            present = any(n.text == node.text for n in result.findall('tag'))
        elif node.tag == 'uniqueid':
            present = any(n.get('type') == node.get('type') for n in result.findall('uniqueid'))
        elif node.tag in ('artist', 'director', 'genre'):
            present = node.tag in original_values or any(n.text == node.text for n in result.findall(node.tag))
        else:
            present = any(n.text and n.text.strip() for n in result.findall(node.tag))
        if not present:
            # Empty elements may be placeholders. Fill the first rather than
            # leaving an empty value ahead of a populated duplicate.
            empty = next((n for n in result.findall(node.tag) if not n.text or not n.text.strip()), None)
            if node.tag in ('tag', 'uniqueid'):
                empty = None
            if empty is not None:
                empty.text = node.text
                empty.attrib.update(node.attrib)
            else:
                result.append(deepcopy(node))
    return result


def plan_nfo(video_path, provider):
    path = Path(video_path)
    target = path.with_suffix('.nfo')
    existing = read_nfo(target)
    title = (existing.findtext('title') if existing is not None else '') or path.stem
    artists = [n.text for n in existing.findall('artist') if n.text] if existing is not None else []
    artist = ', '.join(artists) or path.parent.name
    video = provider.find_by_artist_title(artist, title)
    if not video:
        return target, None, {'file': str(path), 'status': 'no_unique_match', 'title': title, 'artist': artist}
    # Without an original YouTube ID, catalog URLs are references only. Never
    # label them as the source used to download the local file.
    proposed = make_nfo({'title': title}, video)
    merged = merge_missing(existing, proposed)
    ET.indent(merged)
    payload = ET.tostring(merged, encoding='utf-8', xml_declaration=True)
    return target, payload, {
        'file': str(path), 'status': 'proposed', 'title': title, 'artist': artist,
        'imvdb_id': video.get('id'), 'imvdb_page': video.get('url'),
        'match_basis': 'artist_and_title; original YouTube source not verified',
        'year': video.get('year'), 'release_date': video.get('release_date_string'),
    }


def apply_nfo(target, payload, root):
    """Explicit apply only: backup before replacement; confine paths to library."""
    root = Path(root).resolve()
    target = Path(target).resolve()
    if not target.is_relative_to(root):
        raise ValueError('NFO outside library root')
    backup = None
    if target.exists():
        output_mode = stat.S_IMODE(target.stat().st_mode)
    else:
        mask = os.umask(0)
        os.umask(mask)
        output_mode = 0o666 & ~mask
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = target.with_name(target.name + '.bak-' + stamp + '-' + uuid.uuid4().hex[:8])
        with target.open('rb') as original, backup.open('xb') as saved:
            shutil.copyfileobj(original, saved)
    temporary = None
    try:
        with NamedTemporaryFile(dir=target.parent, prefix='.nfo-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        try:
            os.chmod(temporary, output_mode)
        except OSError as error:
            if error.errno not in (errno.EPERM, errno.ENOTSUP, errno.ENOSYS):
                raise
            # TrueNAS NFSv4 ACL inheritance may prohibit chmod.
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return backup


def file_digest(path):
    path = Path(path)
    if not path.exists():
        return None
    if path.stat().st_size > 2_000_000:
        raise ValueError('NFO exceeds size limit')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_current_nfo(video_path, provider, *, genres=None, description=None,
                     mode='refresh', library_root=None):
    """Build the download-time schema without downloading or changing any media."""
    path = Path(video_path)
    target = path.with_suffix('.nfo')
    before = file_digest(target)
    existing = read_nfo(target)
    if existing is not None and existing.tag not in ('musicvideo', 'movie'):
        raise ValueError('Unsupported existing NFO root')
    title = (existing.findtext('title') if existing is not None else '') or path.stem
    artists = [n.text.strip() for n in existing.findall('artist') if n.text and n.text.strip()] if existing is not None else []
    if not artists and (library_root is None or path.parent.resolve() != Path(library_root).resolve()):
        if path.parent.name not in ('Sin artista', 'Unknown Artist'):
            artists = [path.parent.name]
    # Remove only a prefix equal to the known artist, not arbitrary song text.
    if artists and ' - ' in title:
        prefix, remainder = title.split(' - ', 1)
        if _normalized_name(prefix) == _normalized_name(artists[0]):
            title = remainder
    source_values = []
    if existing is not None:
        source_values.extend(n.text for n in existing.findall("uniqueid[@type='youtube']") if n.text)
        for name in ('metube/source_url', 'originalurl'):
            if existing.findtext(name):
                source_values.append(existing.findtext(name))
    infofile = path.with_suffix('.info.json')
    extracted = {}
    if infofile.is_file():
        if library_root is not None and not infofile.resolve().is_relative_to(Path(library_root).resolve()):
            raise ValueError('Info JSON outside library root')
        if infofile.stat().st_size > 2_000_000:
            raise ValueError('Info JSON exceeds size limit')
        extracted = json.loads(infofile.read_text(encoding='utf-8-sig'))
        if str(extracted.get('extractor_key') or extracted.get('extractor') or '').lower().startswith('youtube'):
            source_values.extend([extracted.get('id'), extracted.get('webpage_url')])
    source_ids = youtube_reference_ids({'sources': [dict(source='youtube', source_data=v) for v in source_values if v]})
    if len(source_ids) > 1:
        raise ValueError('Conflicting original YouTube IDs')
    info = {'title': title, 'artists': artists}
    if source_ids:
        info.update(id=source_ids[0], extractor_key='Youtube',
                    webpage_url='https://www.youtube.com/watch?v=' + source_ids[0])
    if extracted.get('duration') is not None:
        info['duration'] = extracted['duration']
    elif existing is not None and re.fullmatch(r'[0-9]+', existing.findtext('runtime') or ''):
        info['duration'] = int(existing.findtext('runtime')) * 60
    errors = []
    video = None
    try:
        if source_ids:
            info['track'] = title
            info['artist'] = artists[0] if artists else ''
            video = provider.lookup(info)
        elif artists:
            video = provider.find_by_artist_title(', '.join(artists), title)
    except Exception as error:
        errors.append('imvdb: ' + _failure_code(error))
    basis = ('exact_youtube_source' if source_ids else 'catalog_artist_title_not_source_verified') if video else 'generic'
    artist = (video.get('artists') or [{}])[0].get('name') if video else (artists[0] if artists else None)
    song = video.get('song_title') if video else title
    if artist and song:
        for name, service, field in [('genre', genres, '_metube_genres'), ('description', description, '_metube_description')]:
            if service is not None:
                try:
                    info[field] = service.lookup(artist, song)
                except Exception as error:
                    errors.append(name + ': ' + _failure_code(error))
    proposed = make_nfo(info, video)
    provenance = proposed.find('metube')
    ET.SubElement(provenance, 'backfill_match_basis').text = basis
    # Catalog sources without an original ID remain labelled references.
    if not source_ids and video:
        references = youtube_reference_ids(video)
        if references:
            plot = proposed.find('plot')
            if plot is None:
                plot = ET.SubElement(proposed, 'plot')
            plot.text = ((plot.text + '\n\n') if plot.text else '') + 'YouTube (referencia de IMVDb): https://www.youtube.com/watch?v=' + references[0]
    # Existing artwork is referenced without renaming, replacing or fetching it.
    for suffix in ('-poster.jpg', '-poster.jpeg', '-poster.png', '-poster.webp', '.jpg'):
        artwork = path.with_name(path.stem + suffix)
        if artwork.is_file() and (library_root is None or artwork.resolve().is_relative_to(Path(library_root).resolve())):
            ET.SubElement(proposed, 'thumb', aspect='poster').text = artwork.name
            break
    if mode == 'missing':
        proposed = merge_missing(existing, proposed)
        proposed.tag = 'musicvideo'
    elif mode != 'refresh':
        raise ValueError('Unsupported regularization mode')
    ET.indent(proposed)
    payload = ET.tostring(proposed, encoding='utf-8', xml_declaration=True)
    report = {'file': str(path), 'status': 'provider_error' if errors else 'proposed',
              'artist': artist, 'title': song, 'match_basis': basis,
              'source_youtube_id': source_ids[0] if source_ids else None,
              'imvdb_id': video.get('id') if video else None,
              'genres': [n.text for n in proposed.findall('genre')],
              'wikipedia_url': proposed.findtext('metube/description_url'),
              'artist_tags': [n.text for n in proposed.findall('tag')],
              'nfo_sha256_before': before, 'video_size': path.stat().st_size,
              'proposed_nfo': payload.decode('utf-8'), 'errors': errors}
    return target, payload, report


def apply_reviewed_entry(entry, root):
    """Apply exact reviewed XML offline, refusing changed files and outside paths."""
    root = Path(root).resolve(strict=True)
    relative = Path(entry['relative_video'])
    if relative.is_absolute() or '..' in relative.parts or ':' in str(relative):
        raise ValueError('Invalid relative video path')
    video = root / relative
    target = video.with_suffix('.nfo')
    if (video.is_symlink() or target.is_symlink()
            or not video.resolve().is_relative_to(root) or not target.resolve().is_relative_to(root)
            or not video.is_file()):
        raise ValueError('Video/NFO outside library or missing')
    if video.stat().st_size != entry['video_size'] or file_digest(target) != entry['nfo_sha256_before']:
        raise ValueError('Video or NFO changed since preview; generate another plan')
    payload = entry['proposed_nfo'].encode('utf-8')
    if len(payload) > 2_000_000 or ET.fromstring(payload).tag != 'musicvideo':
        raise ValueError('Invalid reviewed NFO')
    if entry.get('status') != 'proposed':
        raise ValueError('Only successful reviewed proposals can be applied')
    if target.exists() and target.read_bytes() == payload:
        return {'status': 'unchanged', 'file': str(video)}
    backup = apply_nfo(target, payload, root)
    return {'status': 'applied', 'file': str(video), 'target': str(target),
            'backup': str(backup) if backup else None,
            'nfo_sha256_before': entry['nfo_sha256_before'],
            'nfo_sha256_after': hashlib.sha256(payload).hexdigest()}
