"""User-visible loading behavior with stable, realistically sized products."""
import json
import re
from urllib.parse import urlparse, parse_qs

from PIL import Image, ImageDraw
from playwright.sync_api import expect
import pytest

URL = '/photometry?inst=muscat2&date=261002&target=HAT-P-1&run=r1'


@pytest.fixture(scope='module', autouse=True)
def image_products(live_server):
    night = live_server.root / 'prose/muscat2/261002'
    run = night / '_runs/HAT-P-1/r1'
    run.mkdir(parents=True)
    (run / '_webrun_meta.json').write_text(json.dumps({'run_id': 'r1', 'run_name': 'r1'}))
    def plot(path, size):
        im = Image.new('RGB', size, 'white')
        draw = ImageDraw.Draw(im)
        for y in range(30, size[1], 40):
            draw.line((30, y, size[0]-30, y), fill='blue', width=2)
        im.save(path)
    for key in ('lightcurves', 'raw_flux', 'covariates', 'stacks'):
        plot(run / f'HAT-P-1_muscat2_261002_{key}.png', (1600, 2000))
    for band in ('gp', 'rp'):
        for key in ('ref', 'apertures', 'cutouts', 'alignment'):
            plot(run / f'HAT-P-1_muscat2_{band}_261002_{key}.png', (1200, 900))
        Image.new('RGB', (512, 512), 'red').save(run / f'HAT-P-1_muscat2_{band}_261002.gif',
            save_all=True, append_images=[Image.new('RGB', (512, 512), 'blue')], duration=100, loop=0)
    cal = night.with_name(night.name + '_calibrated')
    cal.mkdir()
    for name in ('master_dark.png', 'master_flat.png'):
        plot(cal / name, (3900, 887))


@pytest.mark.parametrize('width', [1440, 390])
def test_initial_loading_and_mobile_layout(page, width):
    page.set_viewport_size({'width': width, 'height': 844})
    network = page.context.new_cdp_session(page)
    network.send('Network.enable')
    network.send('Network.emulateNetworkConditions', {
        'offline': False, 'latency': 80 if width == 1440 else 150,
        'downloadThroughput': (5_000_000 if width == 1440 else 1_500_000) / 8,
        'uploadThroughput': 750_000 / 8,
    })
    # Override the harness's existing-user preference to exercise first use.
    page.add_init_script("if (!sessionStorage.getItem('tested-first-visit')) { localStorage.removeItem('muscat-chat-min'); sessionStorage.setItem('tested-first-visit', '1'); }")
    page.add_init_script("window.imageCLS = 0; new PerformanceObserver(list => { for (const e of list.getEntries()) if (!e.hadRecentInput) window.imageCLS += e.value; }).observe({type: 'layout-shift', buffered: true});")
    requests = []
    page.on('request', lambda r: requests.append(r.url) if '/api/photometry/file/' in r.url else None)
    page.goto(URL)
    first = page.locator('img[alt="lightcurves"]')
    expect(first).to_be_visible()
    page.wait_for_function("() => document.querySelector('img[alt=lightcurves]').naturalWidth > 0")
    page.wait_for_timeout(500)
    assert not any('master_' in u for u in requests)
    assert not any('.gif' in u and 'preview_width=' not in u for u in requests)
    assert not any('_rp_' in u for u in requests)
    assert all('preview_width=' in u for u in requests)
    assert len(requests) <= 2
    assert page.evaluate('window.imageCLS') < 0.1
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert first.evaluate('(i) => i.getAttribute("width")') == '1600'
    assert first.evaluate('(i) => i.naturalWidth') <= 1560
    if width == 390:
        assert abs(page.locator('#sel-inst').bounding_box()['y'] - page.locator('#sel-date').bounding_box()['y']) < 1
        expect(page.locator('#chat-window')).to_have_class(re.compile(r'\bminimized\b'))
        page.locator('#minimize-chat').click()
        page.reload()
        expect(page.locator('#chat-window')).not_to_have_class(re.compile(r'\bminimized\b'))


def test_scroll_band_animation_and_calibration(page):
    page.goto(URL)
    page.locator('button[data-band="rp"]').click()
    reference = page.locator('.band-panel.active img[alt="rp ref"]')
    reference.scroll_into_view_if_needed()
    page.wait_for_function('(i) => i.naturalWidth > 0', arg=reference.element_handle())
    assert 'preview_width=' in reference.get_attribute('src')
    play = page.locator('.band-panel.active .animation-toggle')
    play.scroll_into_view_if_needed()
    animation = page.locator('.band-panel.active img[alt="rp gif"]')
    page.wait_for_function('(i) => i.naturalWidth > 0', arg=animation.element_handle())
    play.click()
    expect(play).to_have_text('Stop animation')
    assert 'preview_width=' not in animation.get_attribute('src')
    play.click()
    expect(play).to_have_text('Play animation')
    assert 'preview_width=' in animation.get_attribute('src')
    page.locator('.phot-masters summary').click()
    master = page.locator('img[alt="master_flat.png"]')
    master.scroll_into_view_if_needed()
    page.wait_for_function('(i) => i.naturalWidth > 0', arg=master.element_handle())
    assert parse_qs(urlparse(master.get_attribute('src')).query)['v'][0]
    original = master.locator('..').get_attribute('href')
    assert 'preview_width' not in original
    with page.expect_popup() as opened:
        master.locator('..').click()
    popup = opened.value
    popup.wait_for_load_state()
    assert popup.url.endswith(original)
    assert popup.locator('img').evaluate('(i) => i.naturalWidth') == 3900
    popup.close()
