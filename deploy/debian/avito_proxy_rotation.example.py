#!/usr/bin/env python3
"""Ручное переключение на следующий прокси Avito.

Файл выполняет только одно переключение за один запуск. Автоматического таймера
здесь нет. Заполняйте PROXY_URLS и запускайте файл через sudo на Debian-ноутбуке.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

# ВСТАВЛЯЙТЕ ПРОКСИ СЮДА. Каждый адрес обязательно заключается в кавычки.
# Запятую после каждой строки лучше оставлять, включая последнюю строку.
PROXY_URLS = (
    # Вставьте адреса локально: http://login:password@host:port
)

# Этот Bash-скрипт безопасно меняет /etc/avito-bot.env и перезапускает бот.
SWITCH_SCRIPT = Path("/opt/avito-bot/deploy/debian/set_avito_proxy.sh")

# Chromium подключается только к этому локальному порту. Отдельная служба-мост
# отправляет соединение сначала через Mihomo/VPN, а затем через выбранный прокси.
# Данные авторизации передаются купленному прокси отдельным HTTP-заголовком.
# Сам список и системный JSON с реквизитами нельзя публиковать в Git.
LOCAL_AVITO_PROXY_URL = "http://127.0.0.1:7892"
# Если купленный адрес не умеет HTTPS CONNECT, бот остаётся на рабочем VPN.
FALLBACK_VPN_PROXY_URL = "http://127.0.0.1:7890"
ENV_FILE = Path("/etc/avito-bot.env")

# Здесь хранится только номер последнего использованного прокси, без паролей.
STATE_FILE = Path("/var/lib/avito-proxy-rotation/index")
# Служба-мост читает выбранный номер отсюда при каждом новом соединении.
RUNTIME_STATE_FILE = Path("/run/avito-proxy-chain/index")
SUPPORTED_SCHEMES = {"http", "https", "socks5", "socks5h"}


def validate_proxy(proxy_url: str) -> None:
    """Проверить адрес до изменения рабочей конфигурации бота."""

    if "\n" in proxy_url or "\r" in proxy_url:
        raise ValueError("В адресе прокси обнаружен перевод строки")
    parts = urlsplit(proxy_url)
    if parts.scheme not in SUPPORTED_SCHEMES or not parts.hostname or not parts.port:
        raise ValueError(
            "Ожидается полный адрес http(s)://... или socks5(h)://... с портом"
        )


def next_proxy_index(proxy_count: int) -> int:
    """Получить следующий индекс; повреждённое состояние начать с нуля."""

    try:
        previous_index = int(STATE_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return 0
    return (previous_index + 1) % proxy_count


def save_index(index: int) -> None:
    """Атомарно сохранить индекс после успешного переключения."""

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_file = STATE_FILE.with_suffix(".tmp")
    temporary_file.write_text(f"{index}\n", encoding="utf-8")
    temporary_file.chmod(0o600)
    temporary_file.replace(STATE_FILE)

    # /run создаёт systemd для службы-моста. В файле нет адресов или паролей,
    # поэтому пользователь службы может безопасно читать его с правами 0644.
    RUNTIME_STATE_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    runtime_temporary = RUNTIME_STATE_FILE.with_suffix(".tmp")
    runtime_temporary.write_text(f"{index}\n", encoding="utf-8")
    runtime_temporary.chmod(0o644)
    runtime_temporary.replace(RUNTIME_STATE_FILE)


def switch_to(proxy_url: str) -> None:
    """Передать адрес защищённому Bash-переключателю без использования shell."""

    subprocess.run([str(SWITCH_SCRIPT), proxy_url], check=True)


def current_proxy_url() -> str:
    """Прочитать только AVITO_PROXY_URL, не выводя остальные секреты .env."""

    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    for line in lines:
        if line.startswith("AVITO_PROXY_URL="):
            return line.partition("=")[2].strip()
    return ""


def switch_if_needed(proxy_url: str) -> None:
    """Не перезапускать бот, если нужный локальный адрес уже установлен."""

    if current_proxy_url() != proxy_url:
        switch_to(proxy_url)


def purchased_proxy_supports_avito() -> bool:
    """Проверить настоящий HTTPS CONNECT через выбранную цепочку."""

    request = (
        b"CONNECT www.avito.ru:443 HTTP/1.1\r\n"
        b"Host: www.avito.ru:443\r\n"
        b"Proxy-Connection: close\r\n\r\n"
    )
    try:
        with socket.create_connection(("127.0.0.1", 7892), timeout=12) as connection:
            connection.settimeout(12)
            connection.sendall(request)
            response = b""
            while b"\r\n\r\n" not in response and len(response) < 64 * 1024:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response += chunk
    except (OSError, TimeoutError):
        return False
    first_line = response.split(b"\r\n", 1)[0]
    return first_line.startswith(b"HTTP/1.1 200")


def main() -> int:
    """Выполнить ровно одно ручное переключение."""

    if os.geteuid() != 0:
        print("Запустите файл через sudo.", file=sys.stderr)
        return 1
    if not SWITCH_SCRIPT.is_file():
        print(f"Не найден {SWITCH_SCRIPT}.", file=sys.stderr)
        return 1

    # Удобная команда для возврата Avito на прямое соединение.
    if sys.argv[1:] == ["--direct"]:
        switch_to("direct")
        print("Avito возвращён на прямое соединение.")
        return 0
    if sys.argv[1:]:
        print("Допустим только необязательный аргумент --direct.", file=sys.stderr)
        return 2

    if not PROXY_URLS:
        print("Сначала заполните PROXY_URLS в этом Python-файле.", file=sys.stderr)
        return 2

    for proxy_url in PROXY_URLS:
        validate_proxy(proxy_url)

    index = next_proxy_index(len(PROXY_URLS))
    # Сначала сообщаем мосту новый номер, затем перезапускаем Chromium. Новые
    # соединения браузера сразу пойдут через выбранный адрес.
    save_index(index)
    if purchased_proxy_supports_avito():
        # При первом успешном HTTPS-тесте Chromium переключится на цепочку.
        switch_if_needed(LOCAL_AVITO_PROXY_URL)
        result = "включён"
    else:
        # Нерабочий купленный адрес не должен останавливать все поиски.
        switch_if_needed(FALLBACK_VPN_PROXY_URL)
        result = "не поддерживает HTTPS; оставлен резервный VPN"
    # Сам адрес намеренно не печатается: он может содержать пароль.
    print(f"Прокси {index + 1} из {len(PROXY_URLS)}: {result}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
