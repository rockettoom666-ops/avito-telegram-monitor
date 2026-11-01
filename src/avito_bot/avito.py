"""Чтение поисковой выдачи и карточек Avito через Playwright."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from avito_bot.models import Listing
from avito_bot.utils import extract_item_id, parse_published_text

logger = logging.getLogger(__name__)

INVALID_PHOTO_MARKERS = (
    "/icons/",
    "touch-icon",
    "favicon",
    "placeholder",
    "no-photo",
    "nophoto",
)

# Кнопки, которые только раскрывают интерактивную проверку. Селекторы намеренно
# ограничены контейнерами captcha/challenge: бот не нажимает обычные кнопки
# страницы и не пытается выполнять само задание капчи.
CAPTCHA_TRIGGER_SELECTORS = (
    '[data-marker*="captcha" i] button',
    '[data-marker*="challenge" i] button',
    '[id*="captcha" i] button',
    '[id*="challenge" i] button',
    '[class*="captcha" i] button',
    '[class*="challenge" i] button',
    'button:has-text("Пройти проверку")',
    'button:has-text("Начать проверку")',
    'button:has-text("Показать капчу")',
)


def normalize_photo_url(value: str) -> str:
    """Оставить только настоящую HTTP-ссылку на фотографию объявления.

    Иконки, заглушки, data-URL и blob-URL Telegram отправить не сможет, поэтому
    такие значения сразу заменяются пустой строкой.
    """

    photo_url = value.strip()
    if not photo_url or photo_url.startswith(("data:", "blob:")):
        return ""
    absolute = urljoin("https://www.avito.ru", photo_url)
    parts = urlsplit(absolute)
    lowered = absolute.lower()
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    if any(marker in lowered for marker in INVALID_PHOTO_MARKERS):
        return ""
    return absolute


def choose_photo_url(primary: str, fallback: str = "") -> str:
    """Выбрать фото из карточки товара или запасное фото из выдачи."""

    return normalize_photo_url(primary) or normalize_photo_url(fallback)


def playwright_proxy_settings(proxy_url: str) -> dict[str, str] | None:
    """Разделить адрес прокси на сервер и данные авторизации Playwright.

    Chromium не принимает ``login:password`` внутри аргумента
    ``--proxy-server`` и отвечает ``ERR_NO_SUPPORTED_PROXIES``. Playwright,
    напротив, поддерживает отдельные поля ``username`` и ``password``.
    """

    value = proxy_url.strip()
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https", "socks5", "socks5h"}:
        raise AvitoError("Неподдерживаемый протокол прокси Avito")
    try:
        port = parts.port
    except ValueError as error:
        raise AvitoError("Некорректный порт прокси Avito") from error
    if not parts.hostname or port is None:
        raise AvitoError("В адресе прокси Avito отсутствует сервер или порт")

    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    settings = {"server": f"{parts.scheme}://{host}:{port}"}
    if parts.username is not None:
        settings["username"] = unquote(parts.username)
    if parts.password is not None:
        settings["password"] = unquote(parts.password)
    return settings


class AvitoError(RuntimeError):
    """Обычная ошибка загрузки или разбора страницы Avito."""

    pass


class AvitoBlockedError(AvitoError):
    """Avito показал капчу, проверку браузера или ограничение запросов."""

    pass


class AvitoRateLimitError(AvitoBlockedError):
    """Avito временно ограничил прямой IP слишком большим числом запросов."""

    pass


class AvitoScraper:
    """Управляет одним постоянным Chromium-профилем и извлекает объявления.

    Постоянный профиль сохраняет cookies и ручное прохождение капчи между
    перезапусками. Один asyncio.Lock не даёт нескольким проверкам одновременно
    управлять одной и той же вкладкой браузера.
    """

    def __init__(
        self,
        profile_dir: Path,
        *,
        headless: bool = False,
        max_items: int = 100,
        proxy_url: str = "",
    ):
        self.profile_dir = profile_dir
        self.headless = headless
        self.max_items = max_items
        self.proxy_url = proxy_url
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._browser_process: asyncio.subprocess.Process | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._running_headless = headless
        self._manual_window_opened = False
        self._lock = asyncio.Lock()

    @property
    def manual_window_opened(self) -> bool:
        """Сообщить монитору, удалось ли показать последнюю капчу на экране."""

        return self._manual_window_opened

    async def start(self) -> None:
        """Лениво запустить Chromium с сохраняемым пользовательским профилем."""

        if self._context is not None:
            return
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        # После ручного закрытия окна процесс Playwright может ещё оставаться
        # в памяти без рабочего BrowserContext. Перед новым запуском очищаем его.
        if self._playwright is not None:
            await self._stop_playwright()
        self._playwright = await async_playwright().start()
        try:
            if self.headless:
                await self._launch_headless_context()
            else:
                # На Linux видимый Chromium иногда зависает при служебном
                # remote-debugging-pipe Playwright. Локальный CDP-порт работает
                # стабильнее и всё равно доступен только с этого компьютера.
                try:
                    await self._launch_headed_context()
                    self._running_headless = False
                except (AvitoError, OSError, PlaywrightError) as headed_error:
                    # Выключенный/заблокированный экран ноутбука не должен
                    # останавливать поиск. При капче бот ещё раз попробует
                    # открыть видимое окно после входа пользователя в систему.
                    logger.warning(
                        "Visible Chromium is unavailable; using headless mode: %s",
                        headed_error,
                    )
                    self._browser = None
                    self._context = None
                    await self._stop_external_browser()
                    self._remove_stale_profile_files()
                    await self._launch_headless_context()
        except BaseException:
            # Не оставляем зависший driver после ошибки запуска Chromium.
            await self._stop_external_browser()
            await self._stop_playwright()
            raise
        assert self._context is not None
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )
        if not self.headless:
            if not self._running_headless:
                await self._set_window_state(self._page, "minimized")
        logger.info("Playwright Chromium started")

    async def close(self) -> None:
        """Корректно закрыть браузер и процесс Playwright."""

        context = self._context
        browser = self._browser
        self._context = None
        self._page = None
        self._browser = None
        if context is not None:
            try:
                await asyncio.wait_for(context.close(), timeout=10)
            except (PlaywrightError, TimeoutError) as error:
                # Пользователь мог уже закрыть окно целиком — это штатная
                # ситуация, а не причина задерживать остановку службы.
                logger.info("Chromium context was already closed: %s", error)
        if browser is not None:
            try:
                await asyncio.wait_for(browser.close(), timeout=5)
            except (PlaywrightError, TimeoutError) as error:
                logger.info("Chromium browser was already closed: %s", error)
        await self._stop_external_browser()
        await self._stop_playwright()

    async def _launch_headless_context(self) -> None:
        """Запустить устойчивый фоновый Chromium с постоянным профилем."""

        assert self._playwright is not None
        launch_options = {
            "user_data_dir": str(self.profile_dir.resolve()),
            "headless": True,
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
            "viewport": {"width": 1280, "height": 900},
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-notifications",
            ],
            # Не позволяем одному запуску Chromium останавливать мониторинг на
            # стандартные 180 секунд Playwright.
            "timeout": 45_000,
        }
        proxy = playwright_proxy_settings(self.proxy_url)
        if proxy:
            launch_options["proxy"] = proxy
        self._context = await self._playwright.chromium.launch_persistent_context(
            **launch_options,
        )
        self._running_headless = True

    async def _launch_headed_context(self) -> None:
        """Запустить видимый Chromium и подключить Playwright через localhost.

        Chromium записывает выбранный свободный порт в ``DevToolsActivePort``.
        Такой запуск не зависит от проблемного ``remote-debugging-pipe`` и
        сохраняет постоянный профиль для ручного прохождения капчи.
        """

        assert self._playwright is not None
        process_env = await self._active_desktop_environment()
        if process_env is None:
            raise AvitoError("графическая сессия пользователя сейчас не активна")
        proxy = playwright_proxy_settings(self.proxy_url)
        if proxy and (proxy.get("username") or proxy.get("password")):
            # Внешний Chromium через --proxy-server не умеет получать логин и
            # пароль отдельно. Для авторизованного прокси запускаем видимый
            # persistent context непосредственно средствами Playwright.
            await self._launch_authenticated_headed_context(process_env, proxy)
            return
        self._remove_stale_profile_files()
        executable = self._playwright.chromium.executable_path
        args = [
            executable,
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--use-mock-keychain",
            "--disable-notifications",
            "--disable-blink-features=AutomationControlled",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={self.profile_dir.resolve()}",
        ]
        if proxy:
            args.append(f"--proxy-server={proxy['server']}")
        args.append("about:blank")
        self._browser_process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=process_env,
            start_new_session=os.name != "nt",
        )

        port_file = self.profile_dir / "DevToolsActivePort"
        deadline = asyncio.get_running_loop().time() + 30
        port: int | None = None
        while asyncio.get_running_loop().time() < deadline:
            process = self._browser_process
            if process is None:
                raise AvitoError("Процесс Chromium не был создан")
            if process.returncode is not None:
                raise AvitoError(
                    f"Chromium завершился при запуске с кодом {process.returncode}"
                )
            try:
                first_line = port_file.read_text(encoding="utf-8").splitlines()[0]
                port = int(first_line)
                break
            except (FileNotFoundError, IndexError, ValueError, OSError):
                await asyncio.sleep(0.2)
        if port is None:
            raise AvitoError("Chromium не открыл локальный порт за 30 секунд")

        self._browser = await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{port}",
            timeout=15_000,
        )
        if not self._browser.contexts:
            raise AvitoError("Chromium запустился без браузерного контекста")
        self._context = self._browser.contexts[0]

    async def _launch_authenticated_headed_context(
        self,
        process_env: dict[str, str],
        proxy: dict[str, str],
    ) -> None:
        """Запустить видимый Chromium с отдельной proxy-аутентификацией."""

        assert self._playwright is not None
        self._remove_stale_profile_files()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir.resolve()),
            headless=False,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            no_viewport=True,
            proxy=proxy,
            env=process_env,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-notifications",
                "--disable-blink-features=AutomationControlled",
            ],
            timeout=45_000,
        )
        self._running_headless = False

    async def _active_desktop_environment(self) -> dict[str, str] | None:
        """Вернуть окружение активного рабочего стола текущего пользователя."""

        environment = os.environ.copy()
        if os.name == "nt":
            return environment

        try:
            user_process = await asyncio.create_subprocess_exec(
                "loginctl",
                "show-user",
                str(os.getuid()),
                "-p",
                "Display",
                "--value",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            user_output, _ = await asyncio.wait_for(
                user_process.communicate(),
                timeout=3,
            )
            session_id = user_output.decode().strip()
            if not session_id:
                return None

            session_process = await asyncio.create_subprocess_exec(
                "loginctl",
                "show-session",
                session_id,
                "-p",
                "Active",
                "-p",
                "Display",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            session_output, _ = await asyncio.wait_for(
                session_process.communicate(),
                timeout=3,
            )
        except (FileNotFoundError, TimeoutError):
            # На Linux без systemd-logind используем DISPLAY, если он задан.
            return environment if environment.get("DISPLAY") else None

        values = {}
        for line in session_output.decode().splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        if values.get("Active") != "yes" or not values.get("Display"):
            return None
        environment["DISPLAY"] = values["Display"]
        return environment

    def _remove_stale_profile_files(self) -> None:
        """Удалить только служебные файлы завершившегося Chromium.

        Cookies, история и результаты ручной капчи не затрагиваются. Если
        профиль действительно занят живым Chromium, запуск явно отменяется.
        """

        lock_path = self.profile_dir / "SingletonLock"
        if lock_path.is_symlink():
            try:
                lock_value = os.readlink(lock_path)
                pid = int(lock_value.rsplit("-", 1)[-1])
            except (OSError, ValueError):
                pid = 0
            if pid > 0:
                try:
                    cmdline = Path(f"/proc/{pid}/cmdline").read_text(
                        encoding="utf-8",
                        errors="ignore",
                    )
                except OSError:
                    cmdline = ""
                if str(self.profile_dir.resolve()) in cmdline:
                    raise AvitoError(
                        "Профиль Avito уже открыт другим процессом Chromium"
                    )

        for name in (
            "SingletonCookie",
            "SingletonLock",
            "SingletonSocket",
            "DevToolsActivePort",
        ):
            try:
                (self.profile_dir / name).unlink(missing_ok=True)
            except OSError as error:
                raise AvitoError(
                    f"Не удалось очистить служебный файл Chromium {name}"
                ) from error

    async def _stop_external_browser(self) -> None:
        """Завершить созданный ботом Chromium, включая аварийный запуск."""

        process = self._browser_process
        self._browser_process = None
        if process is None or process.returncode is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            await process.wait()
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    async def _stop_playwright(self) -> None:
        """Остановить старый Playwright driver, не зависая на закрытом браузере."""

        playwright = self._playwright
        self._playwright = None
        if playwright is None:
            return
        try:
            await asyncio.wait_for(playwright.stop(), timeout=5)
        except (PlaywrightError, TimeoutError) as error:
            logger.info("Playwright driver was already stopped: %s", error)

    async def _discard_closed_browser(self) -> None:
        """Забыть закрытый пользователем Chromium перед чистым перезапуском."""

        self._context = None
        self._page = None
        self._browser = None
        await self._stop_external_browser()
        await self._stop_playwright()

    async def _set_window_state(self, page: Page, state: str) -> None:
        """Безопасно свернуть или развернуть настоящее окно Chromium."""

        if self._context is None or page.is_closed():
            return
        session = None
        try:
            session = await self._context.new_cdp_session(page)
            window = await session.send("Browser.getWindowForTarget")
            await session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window["windowId"],
                    "bounds": {"windowState": state},
                },
            )
        except PlaywrightError as error:
            logger.info(
                "Could not change Chromium window state to %s: %s",
                state,
                error,
            )
        finally:
            if session is not None:
                try:
                    await session.detach()
                except PlaywrightError:
                    pass

    async def show_for_manual_check(self, page: Page | None = None) -> bool:
        """Развернуть Chromium на физическом экране для ручной капчи.

        Если монитор временно перешёл в фоновый режим из-за заблокированного
        экрана, после входа пользователя Chromium перезапускается с окном.
        """

        self._manual_window_opened = False
        if self.headless:
            return False
        target_page = page or self._page
        if self._context is None or target_page is None or target_page.is_closed():
            return False

        if self._running_headless:
            # Видимое окно можно создать только внутри активной графической
            # сессии пользователя, иначе Chromium снова зависнет при запуске.
            if await self._active_desktop_environment() is None:
                logger.warning(
                    "Manual Avito window was not opened: desktop is not active"
                )
                return False
            target_url = target_page.url
            await self.close()
            await self.start()
            if self._running_headless or self._page is None:
                return False
            target_page = self._page
            try:
                await target_page.goto(
                    target_url,
                    wait_until="commit",
                    timeout=30_000,
                )
            except (PlaywrightError, PlaywrightTimeoutError) as error:
                logger.warning(
                    "Could not reopen Avito verification page: %s",
                    error,
                )

        session = None
        try:
            session = await self._context.new_cdp_session(target_page)
            window = await session.send("Browser.getWindowForTarget")
            window_id = window["windowId"]
            # Chromium не разрешает сразу перейти из minimized в maximized:
            # сначала возвращаем обычное состояние, затем разворачиваем окно.
            await session.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "normal"}},
            )
            await session.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "maximized"}},
            )
            await target_page.bring_to_front()
            self._manual_window_opened = await self._activate_desktop_window()
            if self._manual_window_opened:
                logger.warning(
                    "Chromium window opened for manual Avito verification"
                )
            else:
                logger.warning(
                    "Chromium started, but its window could not be activated"
                )
        except Exception:
            # Ошибка показа окна не должна останавливать остальные поиски.
            logger.exception("Could not open Chromium window for manual verification")
        finally:
            if session is not None:
                try:
                    await session.detach()
                except PlaywrightError:
                    # Окно могли закрыть одновременно с его разворачиванием.
                    pass
        return self._manual_window_opened

    async def open_captcha_prompt(self, page: Page | None = None) -> bool:
        """Нажать только кнопку, которая открывает видимую капчу.

        Метод не распознаёт изображения, не двигает слайдер и не отправляет
        решение. После клика управление остаётся у пользователя на ноутбуке.
        """

        target_page = page or self._page
        if target_page is None or target_page.is_closed():
            return False

        # Иногда кнопка находится внутри iframe GeeTest/другой проверки.
        # Сначала проверяем основную страницу, затем уже загруженные фреймы.
        scopes = [target_page, *target_page.frames]
        for scope in scopes:
            for selector in CAPTCHA_TRIGGER_SELECTORS:
                try:
                    candidate = scope.locator(selector).first
                    if await candidate.count() == 0:
                        continue
                    if not await candidate.is_visible(timeout=1_000):
                        continue
                    await candidate.click(timeout=3_000)
                    logger.warning(
                        "Opened Avito captcha prompt with selector %s",
                        selector,
                    )
                    return True
                except (PlaywrightError, PlaywrightTimeoutError):
                    continue
        logger.info("Avito verification detected, but captcha trigger was not found")
        return False

    async def _activate_desktop_window(self) -> bool:
        """Перенести Chromium на текущий рабочий стол и поднять над терминалом."""

        if os.name == "nt":
            # В Windows bring_to_front из CDP достаточно; wmctrl там отсутствует.
            return True
        environment = await self._active_desktop_environment()
        if environment is None or not environment.get("DISPLAY"):
            return False

        try:
            list_process = await asyncio.create_subprocess_exec(
                "wmctrl",
                "-lx",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
            )
        except FileNotFoundError:
            logger.warning("wmctrl is not installed; Chromium cannot be activated")
            return False
        try:
            output, _ = await asyncio.wait_for(
                list_process.communicate(),
                timeout=3,
            )
        except TimeoutError:
            list_process.kill()
            await list_process.wait()
            return False
        if list_process.returncode != 0:
            return False

        profile_marker = str(self.profile_dir.resolve()).lower()
        window_id = ""
        chromium_fallback = ""
        for line in output.decode(errors="ignore").splitlines():
            lowered = line.lower()
            if "chromium" not in lowered and "google-chrome" not in lowered:
                continue
            candidate = line.split(maxsplit=1)[0]
            chromium_fallback = candidate
            if profile_marker in lowered:
                window_id = candidate
                break
        window_id = window_id or chromium_fallback
        if not window_id:
            return False

        commands = (
            (
                "wmctrl",
                "-i",
                "-r",
                window_id,
                "-b",
                "add,maximized_vert,maximized_horz",
            ),
            ("wmctrl", "-i", "-a", window_id),
        )
        for command in commands:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()
                return False
            if process.returncode != 0:
                return False
        return True

    async def fetch_search(self, url: str) -> list[Listing]:
        """Загрузить первую страницу поиска и получить краткие данные карточек."""

        async with self._lock:
            page = await self._get_page()
            page, response = await self._navigate(page, url)
            await self._ensure_not_blocked(page, response.status if response else None)

            # Современная выдача Avito содержит те же карточки в JSON внутри
            # HTML. Чтение этого JSON быстрее и устойчивее CSS-селекторов, а
            # также часто избавляет от отдельного открытия карточки товара.
            listings = await self._extract_embedded_listings(page)
            if listings:
                logger.info(
                    "Found %d listings in embedded Avito JSON at %s",
                    len(listings),
                    url,
                )
                await asyncio.sleep(random.uniform(1.0, 2.2))
                return listings

            try:
                await page.wait_for_selector(
                    '[data-marker="item"]', state="attached", timeout=20_000
                )
            except PlaywrightTimeoutError:
                await self._ensure_not_blocked(
                    page, response.status if response else None
                )
                empty_marker = page.locator(
                    '[data-marker="search-results"], [data-marker="catalog-serp"]'
                )
                if await empty_marker.count() == 0:
                    raise AvitoError(
                        "Не удалось найти карточки в выдаче Avito"
                    ) from None

            # Небольшая случайная пауза делает обращения менее «роботизированными»
            # и снижает вероятность временной блокировки со стороны Avito.
            listings = await self._extract_cards(page)
            logger.info("Found %d listings at %s", len(listings), url)
            await asyncio.sleep(random.uniform(1.0, 2.2))
            return listings

    async def _extract_embedded_listings(self, page: Page) -> list[Listing]:
        """Прочитать карточки из JSON, встроенного Avito в первую страницу.

        Если Avito изменит формат или JSON отсутствует, метод вернёт пустой
        список. ``fetch_search`` после этого автоматически применит DOM-разбор.
        """

        raw_items = await page.locator(
            'script[type="mime/invalid"][data-mfe-state="true"]'
        ).evaluate_all(
            """
            (scripts, maxItems) => {
                const decodeEntities = (value) => {
                    const textarea = document.createElement('textarea');
                    textarea.innerHTML = value;
                    return textarea.value;
                };
                for (const script of scripts) {
                    const text = script.textContent || '';
                    if (!text || text.includes('sandbox')) continue;
                    for (const candidate of [text, decodeEntities(text)]) {
                        try {
                            const state = JSON.parse(candidate);
                            const items = state?.loaderData?.data?.catalog?.items;
                            if (Array.isArray(items) && items.length) {
                                return items.slice(0, maxItems);
                            }
                        } catch (_) {
                            // На странице бывают другие служебные script-блоки.
                        }
                    }
                }
                return [];
            }
            """,
            self.max_items,
        )
        return self._listings_from_embedded_items(raw_items)

    @staticmethod
    def _listings_from_embedded_items(raw_items: object) -> list[Listing]:
        """Преобразовать нестабильный JSON Avito в наши простые модели."""

        if not isinstance(raw_items, list):
            return []
        listings: list[Listing] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            href = raw.get("urlPath")
            if not isinstance(href, str) or not href:
                continue
            item_url = urljoin("https://www.avito.ru", href).split("?")[0]
            raw_id = raw.get("id")
            fallback_id = str(raw_id) if isinstance(raw_id, (int, str)) else ""
            try:
                item_id = extract_item_id(item_url, fallback_id)
            except ValueError:
                logger.warning("Skipped embedded listing without ID: %s", item_url)
                continue

            title = raw.get("title") if isinstance(raw.get("title"), str) else ""
            description = (
                raw.get("description")
                if isinstance(raw.get("description"), str)
                else ""
            )
            price = AvitoScraper._embedded_price(raw)
            photo_url = AvitoScraper._embedded_photo(raw)
            published_at = AvitoScraper._embedded_timestamp(raw.get("sortTimeStamp"))
            listings.append(
                Listing(
                    item_id=item_id,
                    title=title.strip(),
                    price=price,
                    description=description.strip(),
                    url=item_url,
                    published_at=published_at,
                    photo_url=photo_url,
                    detail_complete=bool(
                        title.strip()
                        and price
                        and description.strip()
                        and photo_url
                        and published_at
                    ),
                )
            )
        return listings

    @staticmethod
    def _embedded_price(raw: dict[object, object]) -> str:
        """Выбрать готовую отображаемую цену из JSON карточки."""

        detailed = raw.get("priceDetailed")
        if isinstance(detailed, dict):
            for key in ("fullString", "string"):
                value = detailed.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        normalized = raw.get("normalizedPrice")
        return normalized.strip() if isinstance(normalized, str) else ""

    @staticmethod
    def _embedded_photo(raw: dict[object, object]) -> str:
        """Найти наиболее крупное доступное изображение карточки."""

        candidates: list[tuple[int, str]] = []
        images = raw.get("images")
        if isinstance(images, list):
            for image in images[:1]:
                if not isinstance(image, dict):
                    continue
                for size, value in image.items():
                    if not isinstance(value, str):
                        continue
                    digits = [
                        int(part)
                        for part in str(size).split("x")
                        if part.isdigit()
                    ]
                    score = digits[0] * digits[1] if len(digits) >= 2 else 0
                    candidates.append((score, value))
        gallery = raw.get("gallery")
        if isinstance(gallery, dict):
            for key in ("imageLargeUrl", "imageUrl"):
                value = gallery.get(key)
                if isinstance(value, str):
                    candidates.append((1, value))
        candidates.sort(reverse=True)
        for _, value in candidates:
            normalized = normalize_photo_url(value)
            if normalized:
                return normalized
        return ""

    @staticmethod
    def _embedded_timestamp(value: object) -> datetime | None:
        """Преобразовать Unix timestamp Avito (миллисекунды или секунды)."""

        if not isinstance(value, (int, float)) or value <= 0:
            return None
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None

    async def _extract_cards(self, page: Page) -> list[Listing]:
        """Извлечь данные всех видимых карточек одним JavaScript-вызовом.

        Выполнение обхода прямо внутри страницы заметно быстрее, чем отдельный
        запрос Playwright для каждого заголовка, цены и изображения.
        """

        raw_items = await page.locator('[data-marker="item"]').evaluate_all(
            """
                (cards, maxItems) => cards.slice(0, maxItems).map((card) => {
                    const one = (...selectors) => {
                        for (const selector of selectors) {
                            const found = card.querySelector(selector);
                            if (found) return found;
                        }
                        return null;
                    };
                    const link = one(
                        'a[data-marker="item-title"]',
                        'a[itemprop="url"]',
                        'a[href*="_"]'
                    );
                    const title = one(
                        '[data-marker="item-title"]',
                        '[itemprop="name"]'
                    );
                    const price = one(
                        '[data-marker="item-price"]',
                        '[itemprop="price"]'
                    );
                    const description = one(
                        '[data-marker="item-specific-params"]',
                        '[data-marker="item-description"]'
                    );
                    const published = one(
                        '[data-marker="item-date"]',
                        '[data-marker="item-date-info"]'
                    );
                    const imageUrl = (image) => {
                        if (!image) return '';
                        for (const attribute of ['srcset', 'data-srcset']) {
                            const srcset = image.getAttribute(attribute) || '';
                            const candidates = srcset.split(',')
                                .map((part) => part.trim().split(/\\s+/)[0])
                                .filter(Boolean);
                            if (candidates.length) {
                                return candidates[candidates.length - 1];
                            }
                        }
                        return image.currentSrc
                            || image.getAttribute('data-src')
                            || image.getAttribute('data-original')
                            || image.src
                            || '';
                    };
                    const image = one(
                        'img[data-marker="image-frame/image"]',
                        'img[itemprop="image"]',
                        'img'
                    );
                    return {
                        id: card.getAttribute('data-item-id') || '',
                        href: link ? link.href : '',
                        title: title ? title.textContent.trim() : '',
                        price: price ? price.textContent.trim() : '',
                        description: description
                            ? description.textContent.trim()
                            : '',
                        publishedText: published
                            ? published.textContent.trim()
                            : '',
                        photoUrl: imageUrl(image)
                    };
                }).filter((item) => item.href);
                """,
            self.max_items,
        )
        # JavaScript возвращает обычные словари. Здесь они проверяются и
        # превращаются в типизированные Listing для остальной части программы.
        listings: list[Listing] = []
        for raw in raw_items:
            item_url = urljoin("https://www.avito.ru", raw["href"]).split("?")[0]
            try:
                item_id = extract_item_id(item_url, raw.get("id", ""))
            except ValueError:
                logger.warning("Skipped listing without ID: %s", item_url)
                continue
            published_text = raw.get("publishedText", "")
            listings.append(
                Listing(
                    item_id=item_id,
                    title=raw.get("title", "").strip(),
                    price=raw.get("price", "").strip(),
                    description=raw.get("description", "").strip(),
                    url=item_url,
                    published_at=parse_published_text(published_text),
                    published_text=published_text,
                    photo_url=normalize_photo_url(raw.get("photoUrl", "")),
                )
            )
        return listings

    async def fetch_detail(self, listing: Listing) -> Listing:
        """Дополнить краткую карточку полным описанием, ценой и главным фото."""

        async with self._lock:
            page = await self._get_page()
            page, response = await self._navigate(page, listing.url)
            await self._ensure_not_blocked(page, response.status if response else None)
            data = await page.evaluate(
                """
                () => {
                    const text = (...selectors) => {
                        for (const selector of selectors) {
                            const found = document.querySelector(selector);
                            if (found && found.textContent) {
                                return found.textContent.trim();
                            }
                        }
                        return '';
                    };
                    const attr = (selector, name) => {
                        const found = document.querySelector(selector);
                        return found ? (found.getAttribute(name) || '') : '';
                    };
                    const imageUrl = (image) => {
                        if (!image) return '';
                        for (const attribute of ['srcset', 'data-srcset']) {
                            const srcset = image.getAttribute(attribute) || '';
                            const candidates = srcset.split(',')
                                .map((part) => part.trim().split(/\\s+/)[0])
                                .filter(Boolean);
                            if (candidates.length) {
                                return candidates[candidates.length - 1];
                            }
                        }
                        return image.currentSrc
                            || image.getAttribute('data-src')
                            || image.getAttribute('data-original')
                            || image.src
                            || '';
                    };
                    const mainImage = document.querySelector(
                        'img[data-marker="image-frame/image"], '
                        + 'img[itemprop="image"], '
                        + '[data-marker="image-frame"] img'
                    );
                    return {
                        title: text(
                            '[data-marker="item-view/title-info"]',
                            'h1[itemprop="name"]',
                            'h1'
                        ),
                        price: text(
                            '[data-marker="item-view/item-price"]',
                            '[itemprop="price"]'
                        ),
                        description: text(
                            '[data-marker="item-view/item-description"]',
                            '[itemprop="description"]'
                        ),
                        publishedText: text(
                            '[data-marker="item-view/item-date"]',
                            'time[itemprop="datePublished"]'
                        ),
                        publishedIso: attr(
                            'time[itemprop="datePublished"]',
                            'datetime'
                        ),
                        photoUrl: imageUrl(mainImage)
                            || attr('meta[property="og:image"]', 'content')
                    };
                }
                """
            )
            # ISO-дата из тега <time> точнее текста «сегодня в ...». Если её
            # нет, используем результат, полученный из поисковой выдачи.
            published_at = listing.published_at
            if data.get("publishedIso"):
                try:
                    published_at = datetime.fromisoformat(
                        data["publishedIso"].replace("Z", "+00:00")
                    )
                except ValueError:
                    pass
            if published_at is None:
                published_at = parse_published_text(data.get("publishedText", ""))
            await asyncio.sleep(random.uniform(1.2, 2.8))
            return Listing(
                item_id=listing.item_id,
                title=data.get("title") or listing.title,
                price=data.get("price") or listing.price,
                description=data.get("description") or listing.description,
                url=listing.url,
                published_at=published_at,
                published_text=data.get("publishedText") or listing.published_text,
                photo_url=choose_photo_url(data.get("photoUrl", ""), listing.photo_url),
            )

    async def download_photo(self, photo_url: str) -> bytes:
        """Скачать фото через браузерный контекст и вернуть готовые байты.

        Avito CDN иногда требует cookies или Referer. Поэтому бот скачивает
        изображение тем же контекстом, которым открыл объявление, а не просит
        Telegram самостоятельно обращаться к URL.
        """

        async with self._lock:
            await self._get_page()
            assert self._context is not None
            try:
                response = await self._context.request.get(
                    photo_url,
                    headers={
                        "Referer": "https://www.avito.ru/",
                        "Accept": (
                            "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
                        ),
                    },
                    timeout=30_000,
                )
            except PlaywrightTimeoutError as error:
                raise AvitoError("Фото Avito не загрузилось за 30 секунд") from error
            if not response.ok:
                raise AvitoError(
                    f"CDN Avito вернул HTTP {response.status} при загрузке фото"
                )
            content_type = response.headers.get("content-type", "").lower()
            if not content_type.startswith("image/"):
                raise AvitoError("Вместо фото Avito вернул другой тип данных")
            content = await response.body()
            if not content or len(content) > 10_000_000:
                raise AvitoError("Фото Avito пустое или превышает 10 МБ")
            return content

    async def _get_page(self) -> Page:
        """Вернуть рабочую вкладку, при необходимости запустив браузер заново."""

        if self._context is None or self._page is None:
            await self.start()
        assert self._page is not None
        if self._page.is_closed():
            assert self._context is not None
            try:
                self._page = await self._context.new_page()
            except PlaywrightError as error:
                logger.warning(
                    "Chromium was closed manually; starting a new browser: %s",
                    error,
                )
                await self._discard_closed_browser()
                await self.start()
                assert self._page is not None
        return self._page

    async def _navigate(self, page: Page, url: str):
        """Открыть URL и один раз восстановить Chromium после зависания.

        ``domcontentloaded`` на тяжёлой странице Avito иногда не наступает,
        хотя сервер уже прислал документ. Поэтому ждём только ``commit``, а
        готовность нужных элементов отдельно проверяют вызывающие методы.
        """

        try:
            response = await page.goto(url, wait_until="commit", timeout=30_000)
            return page, response
        except (PlaywrightError, PlaywrightTimeoutError) as first_error:
            logger.warning(
                "Avito navigation failed; restarting Chromium before retry: %s",
                first_error,
            )

        # Нельзя повторять запрос в той же вкладке: именно она могла зависнуть.
        # Полный перезапуск освобождает сетевую службу и сохраняет cookies,
        # потому что Chromium использует постоянный профиль.
        await self.close()
        await self.start()
        assert self._page is not None
        page = self._page
        try:
            response = await page.goto(url, wait_until="commit", timeout=45_000)
            return page, response
        except (PlaywrightError, PlaywrightTimeoutError) as error:
            raise AvitoError(
                "Avito не ответил даже после перезапуска Chromium"
            ) from error

    async def _ensure_not_blocked(self, page: Page, status_code: int | None) -> None:
        """Распознать 429, капчу и страницу проверки браузера."""

        if status_code == 429:
            raise AvitoRateLimitError(
                "Avito временно ограничил текущий выходной IP "
                "(429 Too Many Requests)"
            )
        # После ``wait_until=commit`` документ уже получен, но JavaScript Avito
        # иногда на несколько секунд удерживает DOM в переходном состоянии.
        # Диагностика капчи не должна из-за этого срывать обычный парсинг.
        try:
            title = (
                await asyncio.wait_for(page.title(), timeout=3)
            ).lower()
        except (TimeoutError, PlaywrightError):
            title = ""
        try:
            body_text = (
                await asyncio.wait_for(
                    page.locator("body").inner_text(timeout=3_000),
                    timeout=4,
                )
            )[:5000].lower()
        except (TimeoutError, PlaywrightError):
            logger.info("Avito body text is not ready; continuing with page data")
            body_text = ""
        markers = (
            "доступ ограничен",
            "подтвердите, что вы не робот",
            "проверка браузера",
            "captcha",
            "слишком много запросов",
        )
        if any(marker in title or marker in body_text for marker in markers):
            await self.show_for_manual_check(page)
            # show_for_manual_check может перезапустить headless Chromium как
            # видимый. Поэтому кликаем уже в актуальной вкладке self._page.
            await self.open_captcha_prompt(self._page or page)
            raise AvitoBlockedError(
                "Avito запросил проверку браузера или прохождение капчи"
            )
