"""The home page widgets and the floating chat window present on every page."""

from __future__ import annotations

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
