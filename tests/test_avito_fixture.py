from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from avito_bot.avito import (
    AvitoRateLimitError,
    AvitoScraper,
    choose_photo_url,
    normalize_photo_url,
    playwright_proxy_settings,
)


@pytest.mark.asyncio
async def test_extracts_search_cards_from_saved_html() -> None:
    fixture = (Path(__file__).parent / "fixtures" / "avito_search.html").read_text(
        encoding="utf-8"
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        # В фикстуре есть внешние картинки. Для разбора DOM не нужно ждать их
        # загрузки из сети — это сделало бы тест зависимым от доступности CDN.
        await page.set_content(fixture, wait_until="domcontentloaded")
        scraper = AvitoScraper(Path(".runtime/test-profile"))
        items = await scraper._extract_cards(page)
        await browser.close()
    assert [item.item_id for item in items] == ["1234567890", "9876543210"]
    assert items[0].title == "MacBook Air M1 16/512"
    assert items[0].price == "75 000 ₽"
    assert items[0].description == "16 ГБ, SSD 512 ГБ"
    assert items[0].photo_url == "https://10.img.avito.st/image/1/macbook-large"
    assert items[0].published_at is not None


@pytest.mark.asyncio
async def test_extracts_complete_cards_from_embedded_avito_json() -> None:
    """Встроенный JSON даёт полную карточку без второго запроса к Avito."""

    fixture = (
        Path(__file__).parent / "fixtures" / "avito_search_json.html"
    ).read_text(encoding="utf-8")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(fixture, wait_until="domcontentloaded")
        scraper = AvitoScraper(Path(".runtime/test-profile"))
        items = await scraper._extract_embedded_listings(page)
        await browser.close()

    assert len(items) == 1
    assert items[0].item_id == "4567890123"
    assert items[0].title == "MacBook Air M1 16/512"
    assert items[0].price == "70 000 ₽"
    assert items[0].description == "Комплект полный, отличное состояние"
    assert items[0].url.endswith("macbook_air_m1_16_512_4567890123")
    assert items[0].photo_url.endswith("json-large")
    assert items[0].published_at is not None
    assert items[0].detail_complete is True


def test_rejects_avito_icons_and_keeps_listing_photo() -> None:
    assert normalize_photo_url("https://m.avito.ru/icons/touch-icon-512x512.png") == ""
    assert (
        choose_photo_url(
            "https://m.avito.ru/icons/touch-icon-512x512.png",
            "https://70.img.avito.st/image/1/real-photo",
        )
        == "https://70.img.avito.st/image/1/real-photo"
    )


def test_authenticated_proxy_is_split_for_playwright() -> None:
    settings = playwright_proxy_settings(
        "http://test%40user:p%3Ass@192.0.2.10:1080"
    )

    assert settings == {
        "server": "http://192.0.2.10:1080",
        "username": "test@user",
        "password": "p:ss",
    }


@pytest.mark.asyncio
async def test_http_429_is_reported_as_ip_rate_limit() -> None:
    scraper = AvitoScraper(Path(".runtime/test-profile"))

    with pytest.raises(AvitoRateLimitError, match="выходной IP"):
        await scraper._ensure_not_blocked(MagicMock(), 429)


@pytest.mark.asyncio
async def test_slow_body_does_not_abort_block_check() -> None:
    """Медленный DOM Avito не должен превращаться в ошибку подписки."""

    scraper = AvitoScraper(Path(".runtime/test-profile"))
    page = MagicMock()
    page.title = AsyncMock(return_value="Avito")
    body = MagicMock()
    body.inner_text = AsyncMock(
        side_effect=PlaywrightTimeoutError("body is still loading")
    )
    page.locator.return_value = body

    await scraper._ensure_not_blocked(page, 200)


@pytest.mark.asyncio
async def test_manual_check_unminimizes_physical_browser() -> None:
    """При капче Chromium должен развернуться и активировать вкладку."""

    scraper = AvitoScraper(Path(".runtime/test-profile"))
    page = MagicMock()
    page.is_closed.return_value = False
    page.bring_to_front = AsyncMock()
    session = AsyncMock()
    session.send.side_effect = [{"windowId": 42}, {}, {}]
    context = MagicMock()
    context.new_cdp_session = AsyncMock(return_value=session)
    scraper._context = context
    scraper._page = page

    await scraper.show_for_manual_check()

    assert session.send.await_args_list == [
        call("Browser.getWindowForTarget"),
        call(
            "Browser.setWindowBounds",
            {"windowId": 42, "bounds": {"windowState": "normal"}},
        ),
        call(
            "Browser.setWindowBounds",
            {"windowId": 42, "bounds": {"windowState": "maximized"}},
        ),
    ]
    page.bring_to_front.assert_awaited_once()
    session.detach.assert_awaited_once()


@pytest.mark.asyncio
async def test_captcha_trigger_button_is_clicked_but_not_solved() -> None:
    """Бот раскрывает капчу и останавливается до ручного решения."""

    scraper = AvitoScraper(Path(".runtime/test-profile"))
    button = MagicMock()
    button.count = AsyncMock(return_value=1)
    button.is_visible = AsyncMock(return_value=True)
    button.click = AsyncMock()
    locator = MagicMock()
    locator.first = button
    page = MagicMock()
    page.is_closed.return_value = False
    page.frames = []
    page.locator.return_value = locator

    opened = await scraper.open_captcha_prompt(page)

    assert opened is True
    button.click.assert_awaited_once_with(timeout=3_000)


@pytest.mark.asyncio
async def test_closed_browser_context_is_restarted() -> None:
    """Закрытие окна пользователем не должно ломать следующие проверки."""

    scraper = AvitoScraper(Path(".runtime/test-profile"))
    stale_page = MagicMock()
    stale_page.is_closed.return_value = True
    stale_context = MagicMock()
    stale_context.new_page = AsyncMock(
        side_effect=PlaywrightError("Target page, context or browser has been closed")
    )
    stale_playwright = MagicMock()
    stale_playwright.stop = AsyncMock()
    scraper._page = stale_page
    scraper._context = stale_context
    scraper._playwright = stale_playwright

    replacement_page = MagicMock()

    async def start_replacement() -> None:
        scraper._page = replacement_page
        scraper._context = MagicMock()
        scraper._playwright = MagicMock()

    scraper.start = AsyncMock(side_effect=start_replacement)

    page = await scraper._get_page()

    assert page is replacement_page
    stale_playwright.stop.assert_awaited_once()
    scraper.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_navigation_timeout_restarts_browser_and_retries() -> None:
    """Зависшая вкладка заменяется новым Chromium перед повторным запросом."""

    scraper = AvitoScraper(Path(".runtime/test-profile"))
    stale_page = MagicMock()
    stale_page.goto = AsyncMock(
        side_effect=PlaywrightTimeoutError("Navigation timeout")
    )
    replacement_page = MagicMock()
    expected_response = MagicMock()
    replacement_page.goto = AsyncMock(return_value=expected_response)

    scraper.close = AsyncMock()

    async def start_replacement() -> None:
        scraper._page = replacement_page
        scraper._context = MagicMock()

    scraper.start = AsyncMock(side_effect=start_replacement)

    page, response = await scraper._navigate(stale_page, "https://www.avito.ru/")

    assert page is replacement_page
    assert response is expected_response
    stale_page.goto.assert_awaited_once_with(
        "https://www.avito.ru/",
        wait_until="commit",
        timeout=30_000,
    )
    scraper.close.assert_awaited_once()
    scraper.start.assert_awaited_once()
    replacement_page.goto.assert_awaited_once_with(
        "https://www.avito.ru/",
        wait_until="commit",
        timeout=45_000,
    )
