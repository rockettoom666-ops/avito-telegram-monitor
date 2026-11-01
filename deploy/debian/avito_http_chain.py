#!/usr/bin/env python3
"""Локальный HTTP-мост: Chromium -> Mihomo/VPN -> купленный HTTP-прокси.

Mihomo умеет быть первым прокси в цепочке, но вложенный ``dialer-proxy`` с
узлами текущей VPN-подписки обрывает TLS. Этот небольшой мост делает то же, что
проверенная команда curl ``--preproxy``: сначала открывает CONNECT до купленного
прокси через Mihomo, затем второй CONNECT до сайта.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
from pathlib import Path

LOGGER = logging.getLogger("avito_proxy_chain")
HEADER_LIMIT = 64 * 1024
CONNECT_TIMEOUT = 20


def read_endpoint(
    endpoints_path: Path,
    index_path: Path,
) -> tuple[str, int, str, str]:
    """Выбрать адрес по индексу; при перезапуске по умолчанию взять первый."""

    endpoints = json.loads(endpoints_path.read_text(encoding="utf-8"))
    if not isinstance(endpoints, list) or not endpoints:
        raise RuntimeError("Список конечных прокси пуст")
    try:
        index = int(index_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        index = 0
    item = endpoints[index % len(endpoints)]
    return (
        str(item["host"]),
        int(item["port"]),
        str(item.get("username", "")),
        str(item.get("password", "")),
    )


def proxy_authorization_header(username: str, password: str) -> bytes:
    """Собрать Basic-заголовок, не записывая реквизиты в журнал."""

    if not username and not password:
        return b""
    token = base64.b64encode(f"{username}:{password}".encode())
    return b"Proxy-Authorization: Basic " + token + b"\r\n"


async def read_headers(reader: asyncio.StreamReader) -> bytes:
    """Прочитать HTTP-заголовки с ограничением размера и времени."""

    data = await asyncio.wait_for(
        reader.readuntil(b"\r\n\r\n"),
        timeout=CONNECT_TIMEOUT,
    )
    if len(data) > HEADER_LIMIT:
        raise RuntimeError("Слишком большие HTTP-заголовки")
    return data


def status_code(headers: bytes) -> int:
    """Достать код из первой строки ответа HTTP-прокси."""

    first_line = headers.split(b"\r\n", 1)[0]
    parts = first_line.split(b" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise RuntimeError("Прокси вернул некорректный HTTP-ответ")
    return int(parts[1])


async def open_proxy_tunnel(
    upstream_host: str,
    upstream_port: int,
    proxy_host: str,
    proxy_port: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Открыть TCP до купленного прокси через локальный Mihomo."""

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(upstream_host, upstream_port),
        timeout=CONNECT_TIMEOUT,
    )
    authority = f"{proxy_host}:{proxy_port}"
    writer.write(
        (
            f"CONNECT {authority} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            "Proxy-Connection: keep-alive\r\n\r\n"
        ).encode("ascii")
    )
    await writer.drain()
    response = await read_headers(reader)
    if status_code(response) != 200:
        writer.close()
        await writer.wait_closed()
        raise RuntimeError("Mihomo не открыл туннель до купленного прокси")
    return reader, writer


async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Передавать байты в одном направлении до закрытия соединения."""

    try:
        while chunk := await reader.read(64 * 1024):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.write_eof()
        except (OSError, RuntimeError):
            pass


async def handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    upstream_host: str,
    upstream_port: int,
    endpoints_path: Path,
    index_path: Path,
) -> None:
    """Обслужить один HTTP или HTTPS-запрос браузера."""

    remote_writer: asyncio.StreamWriter | None = None
    try:
        request = await read_headers(client_reader)
        first_line = request.split(b"\r\n", 1)[0]
        parts = first_line.split(b" ", 2)
        if len(parts) != 3:
            raise RuntimeError("Некорректная первая строка HTTP")
        method = parts[0].upper()
        target = parts[1].decode("ascii", errors="strict")

        proxy_host, proxy_port, proxy_username, proxy_password = read_endpoint(
            endpoints_path,
            index_path,
        )
        authorization = proxy_authorization_header(
            proxy_username,
            proxy_password,
        )
        remote_reader, remote_writer = await open_proxy_tunnel(
            upstream_host,
            upstream_port,
            proxy_host,
            proxy_port,
        )

        if method == b"CONNECT":
            # Второй CONNECT просит уже купленный прокси открыть целевой сайт.
            remote_writer.write(
                (
                    f"CONNECT {target} HTTP/1.1\r\n"
                    f"Host: {target}\r\n"
                    "Proxy-Connection: keep-alive\r\n"
                ).encode("ascii")
                + authorization
                + b"\r\n"
            )
            await remote_writer.drain()
            response = await read_headers(remote_reader)
            if status_code(response) != 200:
                client_writer.write(response)
                await client_writer.drain()
                return
            client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_writer.drain()
        else:
            # Для обычного HTTP исходный абсолютный URL уже подходит прокси.
            if authorization:
                request = request[:-2] + authorization + b"\r\n"
            remote_writer.write(request)
            await remote_writer.drain()

        await asyncio.gather(
            relay(client_reader, remote_writer),
            relay(remote_reader, client_writer),
        )
    except (TimeoutError, OSError, UnicodeError, RuntimeError) as error:
        LOGGER.warning("Proxy connection failed: %s", error)
        if not client_writer.is_closing():
            client_writer.write(
                b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n"
            )
            try:
                await client_writer.drain()
            except ConnectionError:
                pass
    finally:
        if remote_writer is not None:
            remote_writer.close()
            try:
                await remote_writer.wait_closed()
            except ConnectionError:
                pass
        client_writer.close()
        try:
            await client_writer.wait_closed()
        except ConnectionError:
            pass


async def run(args: argparse.Namespace) -> None:
    """Запустить локальный сервер и работать до остановки systemd."""

    server = await asyncio.start_server(
        lambda reader, writer: handle_client(
            reader,
            writer,
            upstream_host=args.upstream_host,
            upstream_port=args.upstream_port,
            endpoints_path=args.endpoints,
            index_path=args.index,
        ),
        args.listen_host,
        args.listen_port,
    )
    LOGGER.info(
        "Avito proxy chain listens on %s:%s",
        args.listen_host,
        args.listen_port,
    )
    async with server:
        await server.serve_forever()


def parse_args() -> argparse.Namespace:
    """Прочитать только служебные адреса; секретов в аргументах нет."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=7892)
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=7890)
    parser.add_argument(
        "--endpoints",
        type=Path,
        default=Path("/etc/avito-proxy-chain-endpoints.json"),
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=Path("/run/avito-proxy-chain/index"),
    )
    return parser.parse_args()


def main() -> None:
    """Настроить журнал и запустить event loop."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
