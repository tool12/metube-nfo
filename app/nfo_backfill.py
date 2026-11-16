"""Merge optional library metadata while preserving existing NFO values."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import os
import shutil
from tempfile import NamedTemporaryFile
import uuid
import xml.etree.ElementTree as ET

from nfo_metadata import make_nfo


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
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = target.with_name(target.name + '.bak-' + stamp + '-' + uuid.uuid4().hex[:8])
        with target.open('rb') as original, backup.open('xb') as saved:
            shutil.copyfileobj(original, saved)
    temporary = None
    try:
        with NamedTemporaryFile(dir=target.parent, prefix='.nfo-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return backup
