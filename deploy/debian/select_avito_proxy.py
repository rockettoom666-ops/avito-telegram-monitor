"""Выбрать из подписки первый VPN-узел без ограничения Avito 429."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

CONTROLLER = "http://127.0.0.1:9090"
GROUP = "VPN"
AVITO_URL = (
    "https://www.avito.ru/nizhniy_novgorod/noutbuki"
    "?q=MacBook%20air%20m1%2016%2F512&s=104"
)


def controller_request(path: str, *, data: dict[str, str] | None = None) -> dict:
    """Вызвать локальный API Mihomo."""

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


def avito_status() -> int:
    """Получить HTTP-код реального поиска через локальный прокси."""

    result = subprocess.run(
        [
            "curl",
            "--silent",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            "--proxy",
            "http://127.0.0.1:7890",
            "--max-time",
            "25",
            "--user-agent",
            (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
            ),
            AVITO_URL,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def select_working_node(max_candidates: int) -> None:
    """Переключать узлы, пока Avito не ответит без 429/сетевой ошибки."""

    group = controller_request(f"/proxies/{quote(GROUP, safe='')}")
    candidates = list(dict.fromkeys(group.get("all", [])))[:max_candidates]
    if not candidates:
        raise RuntimeError("Mihomo не вернул узлы VPN-подписки")

    for index, name in enumerate(candidates, start=1):
        controller_request(
            f"/proxies/{quote(GROUP, safe='')}",
            data={"name": name},
        )
        time.sleep(1)
        status = avito_status()
        print(f"candidate={index}/{len(candidates)} avito_http={status}")
        if 200 <= status < 400:
            print(f"selected_candidate={index}")
            return

    raise RuntimeError("В проверенных VPN-узлах не найден доступный для Avito")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-candidates", type=int, default=50)
    args = parser.parse_args()
    select_working_node(max(1, args.max_candidates))


if __name__ == "__main__":
    main()
