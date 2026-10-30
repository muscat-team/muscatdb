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


def _agent(text):
    """A private @bot reply, shaped like chat._private_agent_payload."""
    return {'id': None, 'user': 'bot', 'text': text, 'kind': 'agent', 'ephemeral': False,
            'private': True, 'ts': 1791000000, 'sid': None, 'mentions': []}


BOT_REPLY = """I am @bot, the read-only assistant.

### ✅ What I Can Do:
* **Explain Code:** modules like `photometry.py` and *how* they fit.
* See TOI-1000 or https://example.org/docs.
  - nested `run_photometry` detail

1. first step
2. second step

```
uv run muscat-db serve
```"""


def test_bot_reply_renders_markdown(page):
    page.goto('/targets')
    page.click('#minimize-chat')
    _receive(page, 'message', _agent(BOT_REPLY))
    body = page.locator('#chat-messages .message.agent .message-body')
    expect(body).to_have_count(1)
    expect(body.locator('.md-heading')).to_have_text('✅ What I Can Do:')
    expect(body.locator('strong').first).to_have_text('Explain Code:')
    expect(body.locator('em')).to_have_text('how')
    expect(body.locator('ul > li')).to_have_count(3)  # two bullets + one nested
    expect(body.locator('ul ul > li')).to_have_text('nested run_photometry detail')
    expect(body.locator('ol > li')).to_have_count(2)
    expect(body.locator('pre code')).to_have_text('uv run muscat-db serve')
    assert body.locator('code').nth(0).inner_text() == 'photometry.py'
    # Linkify and @mentions still apply inside formatted blocks.
    expect(body.locator('a[href="/target?name=TOI-1000"]')).to_have_count(1)
    expect(body.locator('a[href="https://example.org/docs"]')).to_have_count(1)
    expect(body.locator('.chat-mention')).to_have_text('@bot')
    text = body.inner_text()
    assert '**' not in text and '###' not in text and '```' not in text


def test_bot_markdown_stays_inert(page):
    page.goto('/targets')
    page.click('#minimize-chat')
    _receive(page, 'message', _agent(
        '**<img src=x onerror=alert(1)>** [click](javascript:alert(1)) [docs](/jobs) `<b>x</b>`'))
    body = page.locator('#chat-messages .message.agent .message-body')
    expect(body).to_contain_text('<img src=x onerror=alert(1)>')
    assert body.locator('img, b').count() == 0
    assert body.locator('a[href^="javascript"]').count() == 0
    expect(body.locator('a[href="/jobs"]')).to_have_text('docs')
    expect(body.locator('code')).to_have_text('<b>x</b>')


def _private_question(text, ts):
    """The asker's own @bot question, echoed back privately by chat.py."""
    return {'id': None, 'user': 'admin', 'text': text, 'kind': 'user', 'ephemeral': False,
            'private': True, 'ts': ts, 'sid': None, 'mentions': [], 'reactions': [], 'edited': False}


def _bot_texts(page):
    return page.locator('#chat-messages .message.private .message-body').all_inner_texts()


def test_bot_exchange_survives_reload_in_time_order(page):
    page.goto('/targets')
    page.click('#minimize-chat')
    _receive(page, 'message', _private_question('@bot what is finalizing?', 1791000100))
    _receive(page, 'message', {**_agent('It is the **grace** window.'), 'ts': 1791000101})
    # A second tab of the same user receives the same pair; it must not duplicate.
    _receive(page, 'message', {**_agent('It is the **grace** window.'), 'ts': 1791000101})

    page.reload()  # the chat stays expanded across the reload
    messages = page.locator('#chat-messages .message.private')
    expect(messages).to_have_count(2)
    assert _bot_texts(page) == ['@bot what is finalizing?', 'It is the grace window.']
    expect(page.locator('#chat-messages .message.agent strong')).to_have_text('grace')

    # Shared history arriving later interleaves by time with the restored pair.
    before = {'id': 601, 'user': 'observer', 'text': 'earlier', 'ts': 1791000050}
    after = {'id': 602, 'user': 'observer', 'text': 'later', 'ts': 1791000200}
    _receive(page, 'history', {'messages': [before, after]})
    texts = page.locator('#chat-messages .message-body').all_inner_texts()
    assert texts == ['earlier', '@bot what is finalizing?', 'It is the grace window.', 'later']


def test_bot_message_can_be_deleted_and_stays_deleted(page):
    page.goto('/targets')
    page.click('#minimize-chat')
    _receive(page, 'message', _private_question('@bot hi', 1791000300))
    _receive(page, 'message', {**_agent('Hello!'), 'ts': 1791000301})
    reply = page.locator('#chat-messages .message.agent')
    reply.get_by_role('button', name='Delete this message').click()
    expect(reply).to_have_count(0)
    assert _bot_texts(page) == ['@bot hi']

    page.reload()  # the chat stays expanded across the reload
    assert _bot_texts(page) == ['@bot hi']
    page.locator('#chat-messages .message.private').get_by_role('button', name='Delete this message').click()
    expect(page.locator('#chat-messages .message.private')).to_have_count(0)
    page.reload()  # the chat stays expanded across the reload
    expect(page.locator('#chat-messages .message.private')).to_have_count(0)


def test_bot_history_is_kept_per_user(page):
    page.goto('/targets')
    page.click('#minimize-chat')
    _receive(page, 'message', {**_agent('for admin only'), 'ts': 1791000400})
    keys = page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('muscat-bot-chat:'))")
    assert keys == ['muscat-bot-chat:admin']
    # Shared, persisted messages are never copied into the private store.
    _receive(page, 'message', {'id': 605, 'user': 'observer', 'text': 'shared', 'ts': 1791000401})
    stored = page.evaluate("JSON.parse(localStorage.getItem('muscat-bot-chat:admin'))")
    assert [m['text'] for m in stored] == ['for admin only']


def test_teammate_messages_keep_literal_markdown(page):
    page.goto('/targets')
    page.click('#minimize-chat')
    _receive(page, 'message', {'id': 504, 'user': 'observer', 'text': '**not bold** ### nope', 'ts': 1791000000})
    body = page.locator('#chat-messages .message-body').last
    expect(body).to_have_text('**not bold** ### nope')
    assert body.locator('strong').count() == 0


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
    assert sent == [{'user': 'admin', 'text': 'first message', 'room': 'chat:public'}, {'user': 'admin', 'text': 'second message', 'room': 'chat:public'}]


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


def test_chat_room_selection_filters_messages_and_routes_posts(page):
    page.goto('/targets')
    page.click('#minimize-chat')
    page.evaluate("""() => {
        const transport = window.__chatTransport;
        transport.receive('chat_rooms', {rooms: ['chat:public', 'group:unc']});
        transport.receive('history', {messages: [
            {id: 910, kind: 'user', user: 'alice', text: 'Public only', ts: 10},
            {id: 911, kind: 'user', user: 'bob', text: 'UNC conversation', room: 'group:unc', ts: 11}
        ]});
    }""")
    expect(page.get_by_text('Public only', exact=True)).to_be_visible()
    expect(page.get_by_text('UNC conversation', exact=True)).to_be_hidden()
    page.click('#minimize-chat')
    expect(page.locator('#chat-room')).to_be_hidden()
    page.click('#minimize-chat')
    page.select_option('#chat-room', 'group:unc')
    expect(page.get_by_text('Public only', exact=True)).to_be_hidden()
    expect(page.get_by_text('UNC conversation', exact=True)).to_be_visible()
    page.fill('#message-input', 'Hello UNC')
    page.click('#send-button')
    sent = page.evaluate("window.__chatTransport.sent.filter(x => x.event === 'message')")
    assert sent[-1]['data']['room'] == 'group:unc'
    assert sent[-1]['data']['text'] == 'Hello UNC'
    page.reload()
    page.evaluate("window.__chatTransport.receive('chat_rooms', {rooms: ['chat:public', 'group:unc']})")
    expect(page.locator('#chat-room')).to_have_value('group:unc')


def test_chat_room_preference_restored_when_identity_resolves_late(page):
    # /targets is a cached page, so the user name comes from /whoami. Hold it
    # until chat_rooms has been applied to reproduce the CI ordering.
    held = []
    page.goto('/targets')
    page.evaluate("localStorage.setItem('muscat-chat-room:admin', 'group:unc')")
    page.route('**/whoami', lambda route: held.append(route))
    page.reload()
    page.wait_for_function('window.__chatTransport')
    page.evaluate("window.__chatTransport.receive('chat_rooms', {rooms: ['chat:public', 'group:unc']})")
    expect(page.locator('#chat-room')).to_have_value('chat:public')
    for _ in range(100):
        if held:
            break
        page.wait_for_timeout(50)
    assert held, '/whoami was not requested'
    held[-1].fulfill(json={'user': 'admin'})
    expect(page.locator('#chat-room')).to_have_value('group:unc')
