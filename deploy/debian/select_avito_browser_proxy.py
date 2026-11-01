"""Выбрать VPN-узел, на котором реальный Chromium видит карточки Avito."""

from __future__ import annotations

import argparse
import asyncio
import json
from urllib.parse import quote
from urllib.request import Request, urlopen

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

CONTROLLER = "http://127.0.0.1:9090"
GROUP = "VPN"
SEARCH_URL = (
    "https://www.avito.ru/nizhniy_novgorod/noutbuki"
    "?q=MacBook%20air%20m1%2016%2F512&s=104"
)


def controller_request(path: str, *, data: dict[str, str] | None = None) -> dict:
    """Прочитать состояние Mihomo или переключить группу VPN."""

    body = json.dumps(data).encode() if data is not None else None
    request = Request(
        f"{CONTROLLER}{path}",
        data=body,
        method="PUT" if body is not None else "GET",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=10) as response:
        payload = response.read()
    return json.loads(payload) if payload else {}


async def browser_status() -> tuple[int, int]:
    """Вернуть HTTP-код и количество карточек через текущий узел."""

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=False,
            proxy={"server": "http://127.0.0.1:7890"},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = await browser.new_page(locale="ru-RU")
            response = await page.goto(
                SEARCH_URL,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            status = response.status if response else 0
            if status == 200:
                try:
                    await page.wait_for_selector(
                        '[data-marker="item"]',
                        state="attached",
                        timeout=15_000,
                    )
                except PlaywrightTimeoutError:
                    pass
            return status, await page.locator('[data-marker="item"]').count()
        except PlaywrightTimeoutError:
            return 0, 0
        finally:
            await browser.close()


async def select_node(max_candidates: int) -> None:
    """Оставить выбранным первый узел, где появились объявления."""

    group = controller_request(f"/proxies/{quote(GROUP, safe='')}")
    candidates = list(dict.fromkeys(group.get("all", [])))[:max_candidates]
    if not candidates:
        raise RuntimeError("Mihomo не вернул узлы VPN-подписки")

    for index, name in enumerate(candidates, start=1):
        controller_request(
            f"/proxies/{quote(GROUP, safe='')}",
            data={"name": name},
        )
        await asyncio.sleep(1)
        status, items = await browser_status()
        print(
            f"candidate={index}/{len(candidates)} "
            f"avito_http={status} items={items}"
        )
        if status == 200 and items > 0:
            print(f"selected_candidate={index}")
            return

    raise RuntimeError("Ни один проверенный VPN-узел не открыл выдачу Avito")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-candidates", type=int, default=50)
    args = parser.parse_args()
    asyncio.run(select_node(max(1, args.max_candidates)))


if __name__ == "__main__":
    main()
