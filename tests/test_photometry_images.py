"""HTTP image delivery: pixel dimensions, private validation and authorization."""
import io
import os
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from muscat_db import photometry as phot, web


@pytest.fixture
def artifact(mock_db, tmp_path, monkeypatch):
    monkeypatch.setenv('MUSCAT_PROSE_DIR', str(tmp_path / 'prose'))
    monkeypatch.setenv('MUSCAT_TMPDIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(web, '_legacy_artifact_hidden', lambda *a: False)
    monkeypatch.setattr(web, '_run_hidden', lambda *a: False)
    path = Path(str(phot.results_dir('muscat2', '260128')) + '_calibrated') / 'master_flat.png'
    path.parent.mkdir(parents=True)
    Image.new('RGB', (1600, 800), 'white').save(path)
    return TestClient(web.app), path, '/api/photometry/file/muscat2/260128/master_flat.png'


def test_preview_resizes_without_changing_original(artifact):
    client, path, url = artifact
    original = path.read_bytes()
    response = client.get(url, params={'preview_width': 480})
    assert response.status_code == 200
    assert Image.open(io.BytesIO(response.content)).size == (480, 240)
    assert path.read_bytes() == original
    assert client.get(url).content == original
    assert response.headers.get('content-encoding') != 'gzip'


def test_private_etag_revalidation_and_overwrite(artifact):
    client, path, url = artifact
    first = client.get(url + '?preview_width=480&v=old')
    assert first.headers['cache-control'] == 'private, no-cache'
    etag = first.headers['etag']
    cached = client.get(url + '?preview_width=480&v=old', headers={'If-None-Match': 'W/' + etag})
    assert cached.status_code == 304
    assert not cached.content
    before = path.stat().st_mtime_ns
    Image.new('RGB', (1600, 800), 'black').save(path)
    os.utime(path, ns=(before + 1_000_000, before + 1_000_000))
    changed = client.get(url + '?preview_width=480&v=old', headers={'If-None-Match': etag})
    assert changed.status_code == 200
    assert changed.headers['etag'] != etag
    assert Image.open(io.BytesIO(changed.content)).getpixel((0, 0)) == (0, 0, 0)


def test_original_validates_and_access_is_checked_before_304(artifact, monkeypatch):
    client, path, url = artifact
    first = client.get(url)
    assert client.get(url, headers={'If-None-Match': first.headers['etag']}).status_code == 304
    monkeypatch.setattr(web, '_legacy_artifact_hidden', lambda *a: True)
    assert client.get(url, headers={'If-None-Match': first.headers['etag']}).status_code == 404
    assert client.get(url + '?preview_width=480').status_code == 404


def test_run_preview_and_animation_poster(artifact):
    client, path, _ = artifact
    run = phot.run_output_dir('muscat2', '260128', 'TOI06109.02', 'r1')
    run.mkdir(parents=True)
    gif = run / 'TOI06109.02_muscat2_gp_260128.gif'
    Image.new('RGB', (512, 512), 'red').save(gif, save_all=True,
        append_images=[Image.new('RGB', (512, 512), 'blue')], duration=100, loop=0)
    url = '/api/photometry/file/muscat2/260128/TOI06109.02/run/r1/' + gif.name
    poster = client.get(url + '?preview_width=480')
    assert poster.status_code == 200
    assert poster.headers['content-type'] == 'image/png'
    image = Image.open(io.BytesIO(poster.content))
    assert image.size == (480, 480)
    assert not getattr(image, 'is_animated', False)
    assert client.get(url).content == gif.read_bytes()


def test_preview_width_is_bounded(artifact):
    client, _, url = artifact
    assert client.get(url + '?preview_width=99999').status_code == 422


def test_preview_cache_reuses_encoded_bytes(artifact, monkeypatch):
    client, _, url = artifact
    first = client.get(url + '?preview_width=480')
    assert first.status_code == 200
    def unexpected_encode(*args, **kwargs):
        raise AssertionError('unchanged preview was encoded again')
    monkeypatch.setattr(Image.Image, 'save', unexpected_encode)
    second = client.get(url + '?preview_width=480')
    assert second.status_code == 200
    assert second.content == first.content


def test_preview_cache_prunes_to_budget(artifact, monkeypatch):
    from muscat_db import image_delivery
    client, path, url = artifact
    monkeypatch.setattr(image_delivery, '_CACHE_LIMIT', 5000)
    for color in ('red', 'blue', 'green', 'black'):
        Image.new('RGB', (1600, 800), color).save(path)
        assert client.get(url + '?preview_width=480').status_code == 200
    root = path.parents[3] / 'cache/image-previews'
    assert root.is_dir()
    assert list(root.glob('*.png'))
    assert sum(p.stat().st_size for p in root.glob('*.png')) <= 5000
    assert len(list(root.glob('*.png'))) < 4
