#!/usr/bin/env python3
"""Установить локальный мост VPN -> купленный HTTP-прокси на Debian.

Скрипт запускается один раз через sudo. Авторизация хранится только в системном
файле с ограниченными правами и не выводится в терминал или журнал.
"""

from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

MIHOMO_CONFIG = Path("/etc/mihomo/config.yaml")
MIHOMO_BACKUP = Path("/etc/mihomo/config.yaml.backup-before-avito-chain")
ROTATION_PATH = Path("/opt/avito-bot/deploy/debian/avito_proxy_rotation.py")
CHAIN_SCRIPT = Path("/opt/avito-bot/deploy/debian/avito_http_chain.py")
ENDPOINTS_PATH = Path("/etc/avito-proxy-chain-endpoints.json")
SERVICE_PATH = Path("/etc/systemd/system/avito-proxy-chain.service")
BOT_DROPIN = Path("/etc/systemd/system/avito-bot.service.d/proxy-chain.conf")
ROTATION_DROPIN = Path(
    "/etc/systemd/system/avito-proxy-rotation.service.d/proxy-chain.conf"
)

# Эти метки остались от первой попытки использовать встроенный dialer-proxy.
# Установщик удаляет только собственные блоки, не затрагивая VPN-подписку.
MANAGED_BLOCKS = (
    (
        "# BEGIN MANAGED AVITO PURCHASED PROXIES",
        "# END MANAGED AVITO PURCHASED PROXIES",
    ),
    ("# BEGIN MANAGED AVITO LISTENER", "# END MANAGED AVITO LISTENER"),
    ("  # BEGIN MANAGED AVITO GROUP", "  # END MANAGED AVITO GROUP"),
)

SERVICE_UNIT = """[Unit]
Description=Local HTTP proxy chain for Avito
Requires=mihomo.service
After=network-online.target mihomo.service
Before=avito-bot.service

[Service]
Type=simple
User=rockettoom
Group=rockettoom
ExecStart=/usr/bin/python3 /opt/avito-bot/deploy/debian/avito_http_chain.py
Restart=always
RestartSec=3
RuntimeDirectory=avito-proxy-chain
RuntimeDirectoryMode=0755
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
"""

DEPENDENCY_DROPIN = """[Unit]
Requires=avito-proxy-chain.service
After=avito-proxy-chain.service
"""


def load_proxy_endpoints() -> list[dict[str, str | int]]:
    """Прочитать адрес и необязательную авторизацию без вывода в журнал."""

    values = runpy.run_path(str(ROTATION_PATH)).get("PROXY_URLS", ())
    endpoints: list[dict[str, str | int]] = []
    for value in values:
        parts = urlsplit(str(value))
        if parts.scheme != "http" or not parts.hostname or parts.port is None:
            raise RuntimeError("Для локальной цепочки ожидаются HTTP-прокси")
        endpoint: dict[str, str | int] = {
            "host": parts.hostname,
            "port": parts.port,
        }
        if parts.username is not None:
            endpoint["username"] = unquote(parts.username)
        if parts.password is not None:
            endpoint["password"] = unquote(parts.password)
        if any("\r" in str(item) or "\n" in str(item) for item in endpoint.values()):
            raise RuntimeError("Прокси содержит перевод строки")
        endpoints.append(endpoint)
    if not endpoints:
        raise RuntimeError("Список PROXY_URLS пуст")
    return endpoints


def remove_managed_blocks(text: str) -> str:
    """Удалить только секции, ранее добавленные нашим установщиком."""

    for start, end in MANAGED_BLOCKS:
        while start in text:
            before, remainder = text.split(start, 1)
            if end not in remainder:
                raise RuntimeError(f"Не найдена закрывающая метка: {end}")
            _, after = remainder.split(end, 1)
            text = before.rstrip() + "\n\n" + after.lstrip("\r\n")
    return text


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    """Записать системный файл через временный файл и rename."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f"{path.name}.",
        delete=False,
    ) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    try:
        temporary_path.chmod(mode)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def restore_plain_mihomo_config() -> None:
    """Убрать неработающий listener Mihomo и проверить оставшийся конфиг."""

    current = MIHOMO_CONFIG.read_text(encoding="utf-8")
    cleaned = remove_managed_blocks(current)
    stat = MIHOMO_CONFIG.stat()

    if not MIHOMO_BACKUP.exists():
        shutil.copy2(MIHOMO_CONFIG, MIHOMO_BACKUP)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=MIHOMO_CONFIG.parent,
        prefix="config.yaml.chain.",
        delete=False,
    ) as temporary:
        temporary.write(cleaned)
        temporary_path = Path(temporary.name)
    try:
        os.chown(temporary_path, stat.st_uid, stat.st_gid)
        os.chmod(temporary_path, stat.st_mode)
        subprocess.run(
            [
                "/usr/local/bin/mihomo",
                "-t",
                "-d",
                "/var/lib/mihomo",
                "-f",
                str(temporary_path),
            ],
            check=True,
        )
        temporary_path.replace(MIHOMO_CONFIG)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    """Установить конфиг, службу и зависимости автозапуска."""

    if os.geteuid() != 0:
        print("Запустите скрипт через sudo.", file=sys.stderr)
        return 1
    if not CHAIN_SCRIPT.is_file():
        print(f"Не найден {CHAIN_SCRIPT}.", file=sys.stderr)
        return 1

    endpoints = load_proxy_endpoints()
    restore_plain_mihomo_config()
    atomic_write(
        ENDPOINTS_PATH,
        json.dumps(endpoints, ensure_ascii=False, indent=2) + "\n",
        mode=0o640,
    )
    # Служба работает от rockettoom и должна читать файл, но другие
    # пользователи ноутбука не должны видеть логины и пароли прокси.
    shutil.chown(ENDPOINTS_PATH, user="root", group="rockettoom")
    atomic_write(SERVICE_PATH, SERVICE_UNIT)
    atomic_write(BOT_DROPIN, DEPENDENCY_DROPIN)
    atomic_write(ROTATION_DROPIN, DEPENDENCY_DROPIN)

    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "restart", "mihomo.service"], check=True)
    subprocess.run(
        ["systemctl", "enable", "--now", "avito-proxy-chain.service"],
        check=True,
    )
    subprocess.run(
        ["systemctl", "is-active", "--quiet", "avito-proxy-chain.service"],
        check=True,
    )
    print("Локальный мост Avito установлен на 127.0.0.1:7892.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
