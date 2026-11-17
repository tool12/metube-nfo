import pytest
from pathlib import Path
from unittest.mock import Mock

import yt_dlp

from nfo_metadata import NFOMetadataPreProcessor, NFOPostProcessor
from ytdl import _is_within_directory, _sanitize_path_component, _ConfinedYoutubeDL


def preprocessor(tmp_path, video=None):
    pp = NFOMetadataPreProcessor(
        yt_dlp.YoutubeDL({'quiet': True}), provider=Mock(), artist_folders=True,
        directory=tmp_path, sanitize=_sanitize_path_component, within=_is_within_directory)
    pp.provider.lookup.return_value = video
    pp.report_warning = Mock()
    return pp


def test_reuses_existing_artist_folder_case_and_punctuation(tmp_path):
    (tmp_path / 'AC-DC').mkdir()
    pp = preprocessor(tmp_path, {'artists': [{'name': 'AC/DC'}]})
    _, info = pp.run({'title': 'Song'})
    assert info['metube_artist'] == 'AC-DC'
    assert not (tmp_path / 'AC_DC').exists()


def test_uses_primary_artist_for_collaborations(tmp_path):
    pp = preprocessor(tmp_path, {'artists': [{'name': 'Carlos Vives'}, {'name': 'Shakira'}]})
    _, info = pp.run({'title': 'La Bicicleta'})
    assert info['metube_artist'] == 'Carlos Vives'


def test_verified_song_title_avoids_artist_prefix_version_grouping(tmp_path):
    pp = preprocessor(tmp_path, {'song_title': 'Song', 'artists': [{'name': 'Artist'}]})
    original = 'Artist - Song (Official Music Video)'
    _, info = pp.run({'title': original})
    assert info['title'] == 'Song'
    assert info['_metube_source_title'] == original
    assert info['metube_artist'] == 'Artist'


def test_folder_is_sanitized_and_filename_remains_confined(tmp_path):
    pp = preprocessor(tmp_path)
    _, info = pp.run({'title': 'Song', 'artist': '../../outside', 'id': 'id', 'ext': 'mp4'})
    assert '..' not in info['metube_artist']
    assert '/' not in info['metube_artist']
    ydl = _ConfinedYoutubeDL({
        'quiet': True, 'paths': {'home': str(tmp_path)},
        'outtmpl': {'default': '%(metube_artist)s/%(title)s.%(ext)s'},
    }, allowed_roots=(tmp_path,))
    target = Path(ydl.prepare_filename(info)).resolve()
    assert target.is_relative_to(tmp_path)
    assert target.parent.name == info['metube_artist']


def test_missing_artist_uses_fallback_and_does_not_create_directory_early(tmp_path):
    pp = preprocessor(tmp_path)
    pp.provider.lookup.side_effect = TimeoutError
    _, info = pp.run({'title': 'Unidentified'})
    assert info['metube_artist'] == 'Sin artista'
    assert info['_metube_imvdb_checked'] is True
    assert not (tmp_path / 'Sin artista').exists()


def test_sidecar_reuses_metadata_resolved_before_download(tmp_path, monkeypatch):
    monkeypatch.delenv('IMVDB_APP_KEY', raising=False)
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'video')
    post = NFOPostProcessor(yt_dlp.YoutubeDL({'quiet': True}), roots=(tmp_path,),
                            profile='kodi', poster=False, within=_is_within_directory)
    post.provider.lookup = Mock(side_effect=AssertionError('Must not fetch twice'))
    post.run({'filepath': str(video), 'title': 'Song', '_metube_imvdb_checked': True,
              '_metube_imvdb': {'id': 123, 'song_title': 'Song', 'artists': [{'name': 'Artist'}]}})
    assert video.with_suffix('.nfo').exists()
    post.provider.lookup.assert_not_called()


def test_state_folder_is_not_reused_for_artist(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    post = NFOPostProcessor(yt_dlp.YoutubeDL({'quiet': True}), roots=(tmp_path,),
                            profile='kodi', poster=False, within=_is_within_directory, state_dir=state)
    pp = preprocessor(tmp_path, {'artists': [{'name': 'state'}]})
    pp.allowed = post._safe
    _, info = pp.run({'title': 'Song'})
    assert info['metube_artist'] == 'state - artist'


def test_new_artist_directory_and_nfo_follow_real_ytdlp_pipeline(tmp_path, monkeypatch):
    monkeypatch.delenv('IMVDB_APP_KEY', raising=False)
    ydl = _ConfinedYoutubeDL({
        'quiet': True, 'paths': {'home': str(tmp_path)},
        'outtmpl': {'default': '%(metube_artist)s/%(title)s.%(ext)s'},
    }, allowed_roots=(tmp_path,))
    provider = Mock()
    provider.lookup.return_value = {'id': 123, 'song_title': 'Song',
                                    'artists': [{'name': 'New Artist'}]}
    pre = NFOMetadataPreProcessor(ydl, provider=provider, artist_folders=True,
                                 directory=tmp_path, sanitize=_sanitize_path_component,
                                 within=_is_within_directory)
    post = NFOPostProcessor(ydl, roots=(tmp_path,), profile='kodi', poster=False,
                            within=_is_within_directory)
    ydl.add_post_processor(pre, when='pre_process')
    ydl.add_post_processor(post, when='after_move')
    info, _ = ydl.pre_process({'id': 'CD-E-LDc384', 'title': 'Song', 'ext': 'mp4'})
    filename = ydl.prepare_filename(info)
    assert ydl._ensure_dir_exists(filename)
    Path(filename).write_bytes(b'pipeline-fixture')
    ydl.post_process(filename, info)
    assert (tmp_path / 'New Artist' / 'Song.mp4').exists()
    assert (tmp_path / 'New Artist' / 'Song.nfo').exists()
    provider.lookup.assert_called_once()


@pytest.fixture(autouse=True)
def disable_live_genre_network(monkeypatch):
    monkeypatch.setenv('NFO_GENRES_ENABLED', 'false')



def test_musicbrainz_is_third_choice_and_reuses_existing_folder(tmp_path):
    (tmp_path / 'alicia keys').mkdir()
    pp = preprocessor(tmp_path)
    pp.musicbrainz = Mock()
    pp.musicbrainz.resolve_artist.return_value = {'artist': 'Alicia Keys', 'artist_id': 'mb-artist'}
    _, info = pp.run({'title': 'Alicia Keys - No One'})
    assert info['metube_artist'] == 'alicia keys'
    assert info['_metube_musicbrainz']['artist'] == 'Alicia Keys'
    assert [p.name for p in tmp_path.iterdir()] == ['alicia keys']


@pytest.mark.parametrize('source', ['imvdb', 'youtube'])
def test_musicbrainz_does_not_replace_higher_priority_artist(tmp_path, source):
    pp = preprocessor(tmp_path, {'artists': [{'name': 'Catalog Artist'}]} if source == 'imvdb' else None)
    pp.musicbrainz = Mock()
    _, info = pp.run({'title': 'Song', 'artist': 'YouTube Artist'})
    assert info['metube_artist'] == ('Catalog Artist' if source == 'imvdb' else 'YouTube Artist')
    pp.musicbrainz.resolve_artist.assert_not_called()


def test_musicbrainz_failure_keeps_unidentified_fallback(tmp_path):
    pp = preprocessor(tmp_path)
    pp.musicbrainz = Mock()
    pp.musicbrainz.resolve_artist.side_effect = TimeoutError('private message')
    _, info = pp.run({'title': 'Unknown'})
    assert info['metube_artist'] == 'Sin artista'
    assert 'private message' not in str(pp.report_warning.call_args_list)


def test_musicbrainz_artist_remains_confined(tmp_path):
    pp = preprocessor(tmp_path)
    pp.musicbrainz = Mock()
    pp.musicbrainz.resolve_artist.return_value = {'artist': '../../outside'}
    _, info = pp.run({'title': 'Unknown - Song'})
    assert '/' not in info['metube_artist']
    assert '..' not in info['metube_artist']
