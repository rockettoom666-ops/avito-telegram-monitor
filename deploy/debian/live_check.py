"""Разовая проверка Chromium и Avito через локальный VPN-прокси."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from avito_bot.avito import AvitoScraper

SEARCH_URL = (
    "https://www.avito.ru/nizhniy_novgorod/noutbuki"
    "?q=MacBook%20air%20m1%2016%2F512&s=104"
)


async def check(proxy_url: str, *, headless: bool) -> None:
    """Открыть реальный поиск отдельным временным профилем и показать итог."""

    with TemporaryDirectory(prefix="avito-live-check-") as profile:
        scraper = AvitoScraper(
            Path(profile),
            headless=headless,
            max_items=10,
            proxy_url=proxy_url,
        )
        try:
            listings = await scraper.fetch_search(SEARCH_URL)
        finally:
            await scraper.close()

    print(f"avito_live_check=ok listings={len(listings)}")
    if listings:
        print(f"first_item_id={listings[0].item_id}")
        print(f"first_item_has_photo={bool(listings[0].photo_url)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--proxy",
        default="http://127.0.0.1:7890",
        help="Локальный HTTP/SOCKS-прокси Mihomo",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Запустить обычное окно Chromium внутри Xvfb",
    )
    args = parser.parse_args()
    asyncio.run(check(args.proxy, headless=not args.headed))


if __name__ == "__main__":
    main()
