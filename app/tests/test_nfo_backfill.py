from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest

from nfo_backfill import merge_missing, plan_nfo, apply_nfo
from nfo_metadata import IMVDbProvider


def test_merge_preserves_user_values_unknown_fields_and_all_new_credits():
    old = ET.fromstring('<musicvideo><title>My title</title><year>1999</year>'
                        '<artist>My artist</artist><custom>Keep me</custom><premiered/></musicvideo>')
    new = ET.fromstring('<musicvideo><title>Other title</title><year>2000</year>'
                        '<artist>Other artist</artist><premiered>2000-01-02</premiered>'
                        '<director>A</director><director>B</director><tag>Reference</tag></musicvideo>')
    result = merge_missing(old, new)
    assert result.findtext('title') == 'My title'
    assert result.findtext('year') == '1999'
    assert result.findtext('artist') == 'My artist'
    assert result.findtext('custom') == 'Keep me'
    assert result.findtext('premiered') == '2000-01-02'
    assert [n.text for n in result.findall('director')] == ['A', 'B']
    again = merge_missing(result, new)
    assert len(again.findall('tag')) == 1


def test_plan_is_read_only_and_apply_backs_up_exact_original(tmp_path):
    folder = tmp_path / 'Artist'
    folder.mkdir()
    video = folder / 'Song.mp4'
    video.write_bytes(b'video')
    target = video.with_suffix('.nfo')
    original = b'<musicvideo><title>Song</title><artist>Artist</artist></musicvideo>'
    target.write_bytes(original)
    provider = Mock()
    provider.find_by_artist_title.return_value = {
        'id': 123, 'song_title': 'Song', 'year': 2001,
        'artists': [{'name': 'Artist'}], 'sources': [{'source': 'youtube', 'source_data': 'CD-E-LDc384'}]}
    planned, payload, report = plan_nfo(video, provider)
    assert target.read_bytes() == original
    assert list(folder.glob('*.bak-*')) == []
    assert 'not verified' in report['match_basis']
    root = ET.fromstring(payload)
    assert root.find("uniqueid[@type='youtube']") is None
    assert any('youtube.com/watch?v=' in (n.text or '') for n in root.findall('tag'))
    backup = apply_nfo(planned, payload, tmp_path)
    assert backup.read_bytes() == original
    assert ET.parse(target).findtext('year') == '2001'


def test_apply_rejects_path_outside_library(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    with pytest.raises(ValueError):
        apply_nfo(tmp_path / 'outside.nfo', b'<movie/>', library)


def test_title_artist_search_does_not_take_first_unrelated_result():
    provider = IMVDbProvider('test-only', search_enabled=True)
    provider._get = Mock(side_effect=[{'results': [{'id': 123}]},
                                     {'song_title': 'Other song', 'artists': [{'name': 'Artist'}]}])
    assert provider.find_by_artist_title('Artist', 'Song') is None
