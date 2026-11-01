from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from avito_bot.plans import format_search_count
from avito_bot.utils import (
    extract_item_id,
    format_listing_caption,
    format_listing_message,
    normalize_avito_url,
    parse_published_text,
)


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "1 поиск"),
        (2, "2 поиска"),
        (5, "5 поисков"),
        (11, "11 поисков"),
        (21, "21 поиск"),
        (24, "24 поиска"),
        (25, "25 поисков"),
    ],
)
def test_format_search_count_uses_correct_russian_form(
    count: int,
    expected: str,
) -> None:
    assert format_search_count(count) == expected


def test_normalize_avito_url_preserves_filters_and_sorts_by_date() -> None:
    result = normalize_avito_url(
        "https://m.avito.ru/nizhniy_novgorod/noutbuki?q=MacBook+Air&cd=0&p=2"
    )
    assert result.startswith("https://www.avito.ru/nizhniy_novgorod/noutbuki?")
    assert "q=MacBook+Air" in result
    assert "p=2" not in result
    assert "s=104" in result
    assert "cd=0" in result


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/search?q=macbook",
        "javascript:alert(1)",
        "https://avito.ru/",
    ],
)
def test_normalize_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_avito_url(url)


def test_extract_item_id() -> None:
    assert (
        extract_item_id("https://www.avito.ru/moskva/noutbuki/macbook_air_1234567890")
        == "1234567890"
    )
    assert extract_item_id("https://www.avito.ru/item", "555666777") == "555666777"


def test_parse_relative_publication_time() -> None:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=ZoneInfo("UTC"))
    assert parse_published_text("15 минут назад", now).minute == 45
    assert parse_published_text("2 часа назад", now).hour == 6
    assert parse_published_text("только что", now) == now


def test_message_is_html_escaped_and_within_telegram_limit() -> None:
    message = format_listing_message(
        "<MacBook>",
        "70 000 & торг",
        "a" * 10_000,
        "https://www.avito.ru/item?a=1&b=2",
    )
    assert "&lt;MacBook&gt;" in message
    assert "70 000 &amp; торг" in message
    assert len(message) < 4096


def test_photo_caption_is_within_telegram_limit() -> None:
    caption = format_listing_caption(
        "MacBook & аксессуары" * 20,
        "75 000 ₽",
        "<очень длинное описание & детали>" * 500,
        "https://www.avito.ru/item?a=1&b=2",
    )
    assert len(caption) <= 1024
    assert "Открыть объявление" in caption
