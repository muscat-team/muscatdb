"""The home page widgets and the floating chat window present on every page."""

from __future__ import annotations

from playwright.sync_api import expect

# Seven site cards have an LCO weather feed (MuSCAT4 and Sinistro share COJ);
# the OAO and SPM cards have none.
WEATHER_SITES = 7


def _cards(page):
    return page.locator("#weather-cards-grid .weather-card")


def test_weather_cards_show_the_latest_readings(page):
    page.goto("/")
    page.wait_for_function("document.querySelectorAll('#weather-cards-grid .weather-card').length > 0")
    cards = _cards(page)
    texts = cards.all_inner_texts()
    with_readings = [t for t in texts if "12.3°C" in t]
    assert len(with_readings) == WEATHER_SITES
    assert all("3.5 m/s" in t and "41%" in t and "Open" in t for t in with_readings)
    # Sites without an LCO weather feed still get a card, with no readings.
    assert all("—" in t for t in texts if t not in with_readings)


def test_site_buttons_select_one_site(page):
    page.goto("/")
    buttons = page.locator("#site-selector-btns .site-btn")
    buttons.nth(2).click()
    assert page.locator("#site-selector-btns .site-btn.active").all_inner_texts() == ["MuSCAT3 (OGG)"]
    buttons.nth(4).click()
    assert page.locator("#site-selector-btns .site-btn.active").all_inner_texts() == ["Sinistro (LSC)"]


def test_image_lightbox_opens_and_closes(page):
    page.goto("/")
    image = page.locator(".expandable-image").nth(1)
    image.scroll_into_view_if_needed()
    image.click()
    modal = page.locator("#image-modal")
    assert modal.is_visible()
    assert page.inner_text("#modal-caption") == image.get_attribute("alt")
    page.locator("#image-modal button").click()
    assert modal.is_hidden()

    image.click()
    page.mouse.click(5, 5)  # the backdrop
    assert modal.is_hidden()


def test_chat_minimize_state_persists_across_pages(page):
    page.goto("/targets")
    chat = page.locator("#chat-window")
    assert "minimized" in chat.get_attribute("class")

    page.click("#minimize-chat")
    assert "minimized" not in chat.get_attribute("class")
    assert page.locator("#chat-messages").is_visible()

    page.goto("/jobs")
    assert "minimized" not in page.locator("#chat-window").get_attribute("class")
    page.click("#minimize-chat")
    page.goto("/targets")
    assert "minimized" in page.locator("#chat-window").get_attribute("class")


def test_chat_pops_out_into_its_own_window(page):
    page.goto("/targets")
    page.click("#minimize-chat")
    with page.context.expect_page() as popup:
        page.click("#popout-chat")
    pop = popup.value
    pop.wait_for_load_state()
    assert pop.url.endswith("/chat/popout")
    assert pop.locator("#chat-window").get_attribute("data-popout") == "1"
    assert pop.locator("#chat-messages").is_visible()


def _receive(page, event, data=None):
    page.evaluate('([event, data]) => window.__chatTransport.receive(event, data)', [event, data])


def test_weather_failure_shows_missing_readings_without_hanging(page):
    page.route('https://weather-api.lco.global/**', lambda r: r.fulfill(json=[]))
    page.goto('/')
    expect(_cards(page)).to_have_count(8)
    assert all('—' in text for text in _cards(page).all_inner_texts())
    assert not any('12.3°C' in text for text in _cards(page).all_inner_texts())


def test_chat_send_button_enter_and_empty_input(page):
    page.goto('/targets')
    page.click('#minimize-chat')
    page.fill('#message-input', '   ')
    page.click('#send-button')
    assert page.evaluate("__chatTransport.sent.filter(e => e.event === 'message')") == []
    for text, action in [('first message', 'button'), ('second message', 'enter')]:
        page.fill('#message-input', text)
        if action == 'button':
            page.click('#send-button')
        else:
            page.locator('#message-input').press('Enter')
        expect(page.locator('#message-input')).to_have_value('')
    sent = page.evaluate("__chatTransport.sent.filter(e => e.event === 'message').map(e => e.data)")
    assert sent == [{'user': 'admin', 'text': 'first message'}, {'user': 'admin', 'text': 'second message'}]


def test_chat_history_live_edit_delete_and_reconnect(page):
    page.goto('/targets')
    page.click('#minimize-chat')
    message = {'id': 501, 'user': 'observer', 'text': 'first observation', 'ts': 1791000000}
    _receive(page, 'history', {'messages': [message]})
    expect(page.locator('#chat-messages')).to_contain_text('first observation')
    _receive(page, 'message', {**message, 'id': 502, 'text': 'live observation'})
    expect(page.locator('#chat-messages .message-body')).to_have_count(2)
    _receive(page, 'message_edited', {'id': 502, 'text': 'corrected observation'})
    expect(page.locator('#chat-messages')).to_contain_text('corrected observation')
    _receive(page, 'message_deleted', {'id': 501})
    expect(page.locator('#chat-messages .message-body')).to_have_count(1)
    _receive(page, 'disconnect')
    expect(page.locator('#chat-status-dot')).to_have_attribute('title', 'Disconnected')
    _receive(page, 'connect')
    expect(page.locator('#chat-status-dot')).to_have_attribute('title', 'Connected')
    _receive(page, 'presence', {'users': ['admin', 'observer'], 'count': 2})
    expect(page.locator('#chat-online')).to_have_text('2 online')


def test_chat_target_link_and_literal_html(page):
    page.goto('/targets')
    page.click('#minimize-chat')
    _receive(page, 'message', {'id': 503, 'user': 'observer', 'text': 'TOI-1000 <img src=x onerror=alert(1)>', 'ts': 1791000000})
    expect(page.locator('#chat-messages')).to_contain_text('<img src=x onerror=alert(1)>')
    assert page.locator('#chat-messages img').count() == 0
    link = page.locator('#chat-messages a[href^="/target?"]')
    expect(link).to_have_attribute('href', '/target?name=TOI-1000')
    link.click()
    page.wait_for_url('**/target?name=TOI-1000')
