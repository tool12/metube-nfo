"""Sidecars must never corrupt a download or attach metadata to another video."""
from pathlib import Path
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest
import yt_dlp

from nfo_metadata import IMVDbProvider, NFOPostProcessor, make_nfo, public_video_id, imvdb_video_path
from ytdl import _is_within_directory, DownloadInfo
from download_options import DownloadOptions


INFO = {'id': 'CD-E-LDc384', 'extractor_key': 'Youtube',
        'title': 'Metallica: Enter Sandman', 'description': 'Original <description> & text',
        'upload_date': '20120319', 'duration': 331,
        'webpage_url': 'https://www.youtube.com/watch?v=CD-E-LDc384'}
VIDEO = {'id': 123, 'song_title': 'Enter Sandman', 'year': 1991,
         'artists': [{'name': 'Metallica'}],
         'directors': [{'entity_name': 'Wayne Isham'}],
         'sources': [{'source': 'youtube', 'source_data': 'CD-E-LDc384'}]}


def test_generic_does_not_mislabel_upload_as_premiere():
    root = make_nfo(INFO)
    assert root.tag == 'musicvideo'
    assert root.find('premiered') is None
    assert root.find('year') is None
    assert root.findtext('plot') == INFO['description']
    assert root.findtext('uniqueid') == INFO['id']
    ET.fromstring(ET.tostring(root))


def test_verified_video_has_original_year_and_credits():
    root = make_nfo(INFO, VIDEO)
    assert root.tag == 'musicvideo'
    assert root.findtext('year') == '1991'
    assert root.findtext('director') == 'Wayne Isham'
    assert root.findtext('artist') == 'Metallica'
    assert root.find('premiered') is None
    assert root.find("uniqueid[@type='imvdb']").text == '123'
    assert 'YouTube reference from IMVDb: https://www.youtube.com/watch?v=CD-E-LDc384' in [
        tag.text for tag in root.findall('tag')]


def test_full_premiere_date_is_used_only_when_supplied():
    root = make_nfo(INFO, dict(VIDEO, release_date_string='July 30, 1991'))
    assert root.findtext('premiered') == '1991-07-30'
    root = make_nfo(INFO, dict(VIDEO, release_date_string='1991'))
    assert root.find('premiered') is None


def test_explicit_id_bypasses_search_but_still_requires_source_match():
    provider = IMVDbProvider('test-only')
    provider._get = Mock(return_value=VIDEO)
    assert provider.lookup(INFO, '123') == VIDEO
    provider._get.assert_called_once_with('video/123', include='sources,credits')


def test_provider_requires_exact_source_and_caches(tmp_path):
    provider = IMVDbProvider('test-only', tmp_path, search_enabled=True)
    provider._get = Mock(side_effect=[{'results': [{'id': 123}]}, VIDEO])
    assert provider.lookup(INFO) == VIDEO
    assert provider.lookup(INFO) == VIDEO
    assert provider._get.call_count == 2
    assert 'test-only' not in (tmp_path / (INFO['id'] + '.json')).read_text()


@pytest.mark.parametrize('video', [dict(VIDEO, sources=[]), dict(VIDEO, sources=[
    {'source': 'youtube', 'source_data': 'another-id'}])])
def test_title_match_is_not_enough(video):
    provider = IMVDbProvider('test-only', search_enabled=True)
    provider._get = Mock(side_effect=[{'results': [{'id': 123}]}, video])
    assert provider.lookup(INFO) is None


def processor(tmp_path, monkeypatch, poster=True):
    monkeypatch.delenv('IMVDB_APP_KEY', raising=False)
    pp = NFOPostProcessor(yt_dlp.YoutubeDL({'quiet': True}), roots=(tmp_path,),
                          profile='kodi', poster=poster, within=_is_within_directory)
    pp.report_warning = Mock()
    return pp


def test_provider_failure_still_writes_generic_and_local_poster(tmp_path, monkeypatch):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'video')
    thumb = tmp_path / 'video.jpg'
    thumb.write_bytes(b'example-image')
    pp = processor(tmp_path, monkeypatch)
    pp.provider.lookup = Mock(side_effect=TimeoutError)
    info = dict(INFO, filepath=str(video), thumbnails=[{'filepath': str(thumb)}])
    assert pp.run(info) == ([], info)
    root = ET.parse(video.with_suffix('.nfo')).getroot()
    assert root.tag == 'musicvideo'
    assert root.findtext('thumb') == 'video-poster.jpg'
    assert (tmp_path / 'video-poster.jpg').read_bytes() == b'example-image'
    assert video.read_bytes() == b'video'


def test_existing_nfo_and_cover_are_preserved(tmp_path, monkeypatch):
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'video')
    nfo = video.with_suffix('.nfo')
    nfo.write_bytes(b'user metadata')
    pp = processor(tmp_path, monkeypatch)
    pp.run(dict(INFO, filepath=str(video)))
    assert nfo.read_bytes() == b'user metadata'


def test_outside_directory_is_rejected(tmp_path, monkeypatch):
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    outside = tmp_path / 'outside.mp4'
    outside.write_bytes(b'video')
    pp = processor(allowed, monkeypatch)
    pp.run(dict(INFO, filepath=str(outside)))
    assert not outside.with_suffix('.nfo').exists()
    pp.report_warning.assert_called()


def test_selection_survives_retry_and_old_state():
    options = DownloadOptions(nfo_profile='kodi', nfo_poster=False, nfo_imvdb_id='123')
    info = DownloadInfo(id='i', title='t', url='u', error=None, entry=None,
                        **options.download_info_fields())
    assert DownloadOptions.from_info(info) == options


def test_real_ytdlp_pipeline_moves_video_and_thumbnail_then_writes_nfo(tmp_path, monkeypatch):
    final = tmp_path / 'final'
    temporary = tmp_path / 'temporary'
    final.mkdir()
    temporary.mkdir()
    video = temporary / 'video.mp4'
    thumb = temporary / 'video.jpg'
    video.write_bytes(b'test-video')
    thumb.write_bytes(b'test-thumbnail')
    pp = processor(final, monkeypatch)
    ydl = pp._downloader
    ydl.add_post_processor(pp, when='after_move')
    info = dict(INFO, __finaldir=str(final), thumbnails=[{
        'filepath': str(thumb), 'url': 'https://example.com/thumbnail.jpg'}])
    ydl.post_process(str(video), info, {str(thumb): str(final / thumb.name)})
    assert (final / video.name).read_bytes() == b'test-video'
    nfo = ET.parse(final / 'video.nfo').getroot()
    assert nfo.findtext('thumb') == 'video-poster.jpg'


def test_request_validation_and_subscription_keep_nfo_options():
    import main
    from aiohttp import web
    from subscriptions import SubscriptionInfo, _subscription_to_record
    request = dict(url='https://www.youtube.com/watch?v=CD-E-LDc384',
                   download_type='video', quality='best', format='any',
                   nfo_profile='kodi', nfo_poster=False, nfo_imvdb_id='101313349910')
    _, options = main.parse_download_options(request)
    assert options.nfo_profile == 'kodi'
    assert options.nfo_poster is False
    assert options.nfo_imvdb_id == '101313349910'
    with pytest.raises(web.HTTPBadRequest):
        main.parse_download_options(dict(request, nfo_profile='unknown'))
    with pytest.raises(web.HTTPBadRequest):
        main.parse_download_options(dict(request, nfo_imvdb_id='../escape'))
    sub = SubscriptionInfo(id='s', name='test', url=request['url'],
                           nfo_profile='kodi', nfo_poster=False)
    assert sub.download_options().nfo_profile == 'kodi'
    record = _subscription_to_record(sub)
    assert record['nfo_poster'] is False
    assert sub.download_options().nfo_imvdb_id == ''


def test_suspended_search_does_not_call_api():
    provider = IMVDbProvider('test-only', web_search_enabled=False)
    provider._get = Mock()
    assert provider.lookup(INFO) is None
    provider._get.assert_not_called()


def test_private_key_is_not_exposed_to_browser(monkeypatch):
    import main
    monkeypatch.setenv('IMVDB_APP_KEY', 'private-test-key')
    config = main.Config()
    assert config.IMVDB_APP_KEY == 'private-test-key'
    assert 'IMVDB_APP_KEY' not in config.frontend_safe()
    assert 'private-test-key' not in repr(config.frontend_safe())


def test_web_discovery_gets_id_then_verifies_youtube_source():
    provider = IMVDbProvider('test-only')
    provider._get_html = Mock(side_effect=[
        '<a href="https://imvdb.com/video/metallica/enter-sandman">Enter Sandman</a>'
        '<a href="https://evil.example/video/metallica/enter-sandman">untrusted</a>',
        '<p>ID: 123</p>'])
    provider._get = Mock(return_value=VIDEO)
    assert provider.lookup(INFO) == VIDEO
    provider._get_html.assert_any_call('/search', search_term='Metallica: Enter Sandman')
    provider._get.assert_called_once_with('video/123', include='sources,credits')


def test_web_result_with_wrong_source_is_rejected():
    provider = IMVDbProvider('test-only')
    provider._get_html = Mock(side_effect=[
        '<a href="/video/metallica/enter-sandman">Enter Sandman</a>', '<p>ID: 123</p>'])
    provider._get = Mock(return_value=dict(VIDEO, sources=[]))
    assert provider.lookup(INFO) is None


def test_explicit_public_url_resolves_to_id():
    provider = IMVDbProvider('test-only')
    provider._get_html = Mock(return_value='<p>ID: 123</p>')
    provider._get = Mock(return_value=VIDEO)
    assert provider.lookup(INFO, 'https://imvdb.com/video/metallica/enter-sandman') == VIDEO
    provider._get_html.assert_called_once_with('/video/metallica/enter-sandman')


def test_missing_or_ambiguous_public_id_is_not_guessed():
    assert public_video_id('<script>FI.video_id="6192";</script>') is None
    assert public_video_id('<p>ID: 123</p><p>ID: 456</p>') is None


@pytest.mark.parametrize('url', ['https://evil.example/video/a/b',
                               'https://imvdb.com.evil.example/video/a/b',
                               'https://user:pass@imvdb.com/video/a/b',
                               'http://127.0.0.1/video/a/b', 'https://imvdb.com/login'])
def test_discovery_cannot_target_other_hosts_or_paths(url):
    assert imvdb_video_path(url) is None


def test_public_requests_do_not_send_app_key():
    provider = IMVDbProvider('private-test-key')
    provider._read = Mock(return_value=b'<html></html>')
    provider._get_html('/search', search_term='artist song')
    assert 'IMVDB-APP-KEY' not in provider._read.call_args.args[1]

