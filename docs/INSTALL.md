# Полная установка и обслуживание

## Требования

- Python **3.12 или 3.13**; рекомендуем 3.12. Python 3.11 и 3.14 не подходят.
- Git либо скачанный и распакованный ZIP репозитория.
- Токен нового Telegram-бота от `@BotFather` (`/newbot`).
- Интернет с доступом к Telegram Bot API и Avito.
- Для ручной капчи — рабочий стол с графической сессией.

Один токен должен обслуживаться одним процессом polling. Перед переносом бота
на другой компьютер остановите старый экземпляр. Домен и входящий HTTPS-порт
для этой версии не нужны: бот получает обновления через long polling.

## Windows

### 1. Установить Python и скачать код

Установите [Python 3.12](https://www.python.org/downloads/) с Python Launcher
и опцией Add Python to PATH, а также [Git](https://git-scm.com/downloads).
Откройте PowerShell:

```powershell
py -3.12 --version
git clone https://github.com/rockettoom666-ops/avito-telegram-monitor.git
cd avito-telegram-monitor
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

Для приватного репозитория GitHub запросит авторизацию. Вместо клонирования
можно выбрать Code → Download ZIP на GitHub и распаковать архив.
Установщик создаёт `.venv`, ставит проект в editable-режиме и скачивает Chromium.
Он не запускает бота и не заполняет токен за вас.

### 2. Создать .env

Только при первой установке, когда `.env` ещё отсутствует:

```powershell
Copy-Item .env.example .env
notepad .env
```

Минимально заполните `TELEGRAM_BOT_TOKEN`. Все параметры описаны ниже.
Если токен когда-либо был опубликован, перевыпустите его в BotFather.

### 3. Запустить и проверить

```powershell
.\.venv\Scripts\python.exe -m avito_bot
```

В Telegram нажмите Start, отправьте `/id`, настройте [администратора](ADMIN.md).
Проверьте `/subscription`, отправьте ссылку поиска и выберите интервал.
В `/status` появится результат проверки. При первой выдаче старые объявления
запоминаются без уведомлений. Завершение консольного запуска — Ctrl+C.

Для запуска и остановки двойным щелчком используйте файлы из папки `старт`.

### 4. Автозапуск при входе в Windows

Закройте вручную запущенный процесс, затем выполните:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
```

В Планировщике появится задача **Avito Telegram Monitor**. Скрипт также сразу
запустит её. Используйте один экземпляр проекта на одну такую задачу.
Автозапуск работает после входа пользователя в Windows, а не до входа.
Если Windows запрещает регистрацию задачи, откройте PowerShell от имени
администратора той же учётной записи и повторите команду.

Удалить автозапуск:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall_autostart.ps1
```

## Linux (Debian/Ubuntu)

Ниже новая установка под своим обычным пользователем в
`~/avito-telegram-monitor`. Она не требует Mihomo, конкретного имени пользователя
или установки в `/opt`. Не применяйте одновременно пользовательскую службу и
старую системную службу с тем же токеном.

### 1. Установить системные пакеты и подходящий Python

Для Debian/Ubuntu:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip wmctrl
python3 --version
```

Нужна версия 3.12 или 3.13. При другой версии установите подходящий
интерпретатор отдельно, например через [uv](https://docs.astral.sh/uv/getting-started/installation/).
Не заменяйте системный `/usr/bin/python3`. В следующих командах вместо
`python3` используйте путь к установленному Python 3.12/3.13.

### 2. Скачать проект и создать окружение

```bash
git clone https://github.com/rockettoom666-ops/avito-telegram-monitor.git "$HOME/avito-telegram-monitor"
cd "$HOME/avito-telegram-monitor"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
sudo .venv/bin/python -m playwright install-deps chromium
.venv/bin/python -m playwright install chromium
cp .env.example .env
chmod 600 .env
nano .env
```

Последняя команда Playwright скачивает Chromium **от вашего пользователя**.
Не запускайте её через sudo: иначе браузер попадёт в кэш root.
`install-deps` с sudo устанавливает только системные библиотеки браузера.

### 3. Выбрать режим браузера

На ноутбуке с рабочим столом оставьте `AVITO_HEADLESS=false`. Запускайте из
терминала активной графической сессии. Для активации окна нужен `wmctrl` и X11;
под Wayland перенос окна на передний план может не поддерживаться.

На сервере без графики укажите `AVITO_HEADLESS=true`. Проверки будут работать
в фоне, но для прохождения капчи потребуется графическая сессия с тем же
профилем Chromium. Бот не решает задания капчи автоматически.

### 4. Первый запуск

```bash
cd "$HOME/avito-telegram-monitor"
.venv/bin/python -m avito_bot
```

Проверьте команды в Telegram, настройте [админа](ADMIN.md), добавьте поиск.
Затем остановите процесс Ctrl+C, прежде чем включать автозапуск.

### 5. Автозапуск через systemd пользователя

Готовая служба рассчитана именно на каталог `~/avito-telegram-monitor`.
Если вы выбрали другой каталог, сначала исправьте пути в unit-файле.

```bash
mkdir -p "$HOME/.config/systemd/user"
cp deploy/linux/avito-bot.service "$HOME/.config/systemd/user/avito-bot.service"
systemctl --user daemon-reload
```

Для видимого Chromium выполните из терминала рабочего стола:

```bash
systemctl --user import-environment DISPLAY XAUTHORITY WAYLAND_DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS
```

У неиспользуемых графических переменных могут быть предупреждения. Затем:

```bash
systemctl --user enable --now avito-bot
systemctl --user status avito-bot --no-pager
journalctl --user -u avito-bot -n 50 --no-pager
```

Служба запускается при входе пользователя и перезапускается после сбоя.
На сервере для запуска без интерактивного входа можно отдельно включить
`sudo loginctl enable-linger "$USER"` и установить `AVITO_HEADLESS=true`.
Для видимого браузера всё равно нужен вход в рабочий стол.

Управление:

```bash
systemctl --user stop avito-bot
systemctl --user start avito-bot
systemctl --user restart avito-bot
systemctl --user disable --now avito-bot
```

### Если используется прежняя установка в /opt

Она читает `/etc/avito-bot.env` и управляется командой
`sudo systemctl restart avito-bot`. Файлы `deploy/debian` относятся к этой схеме.
Они содержат примеры с пользователем `rockettoom`, путями `/opt/avito-bot` и
локальным Mihomo. Для новой машины используйте инструкцию выше; сведения о
старой схеме — в [deploy/debian/README.md](../deploy/debian/README.md).

## Параметры .env

| Параметр | Назначение |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Обязательный токен BotFather. |
| `ADMIN_CHAT_IDS` | Числовые ID администраторов через запятую; узнать через `/id`. |
| `MAX_SUBSCRIPTIONS_PER_USER` | Общий верхний лимит поисков и лимит админа, по умолчанию 10. |
| `DATABASE_PATH` | SQLite, по умолчанию `data/bot.sqlite3`. |
| `LOG_PATH` | Ротационный лог, по умолчанию `logs/bot.log`. |
| `AVITO_PROFILE_DIR` | Постоянный профиль, по умолчанию `.runtime/avito-profile`. |
| `AVITO_HEADLESS` | `false` для окна; `true` для фона без графики. |
| `AVITO_MAX_ITEMS` | Максимум разбираемых карточек с первой страницы, по умолчанию 100. |
| `PROXY_URL` | Прокси только Telegram; пустое значение — напрямую. |
| `AVITO_PROXY_URL` | Прокси Chromium; задаётся независимо от Telegram. |
| `BASIC_PLAN_STARS`, `PRO_PLAN_STARS` | Цены недельных тарифов в Stars. |
| `LIFETIME_PLAN_RUBLES`, `LIFETIME_PLAN_STARS` | Цены бессрочного тарифа. |
| `SUPPORT_CONTACT`, `MERCHANT_NAME` | Контакт поддержки и имя продавца в условиях. |
| `YOOKASSA_PROVIDER_TOKEN` | Provider token Telegram, при отсутствии бот запускается без рублёвых счетов. |
| `YOOKASSA_SEND_RECEIPT`, `YOOKASSA_VAT_CODE` | Параметры чеков существующей интеграции. |
| `YOOKASSA_TEST_PRICE_RUBLES` | Сумма административной тестовой кнопки, код требует не меньше 88 ₽. |

На Linux разрешите чтение `.env` только владельцу. Режим `600` задаётся выше.
База и браузерный профиль также содержат пользовательские данные.

## Обновление

Сначала остановите бот и сделайте резервную копию. Не копируйте `.env.example`
поверх существующего `.env`: иначе потеряются токен и админы.

Windows из каталога проекта:

```powershell
.\старт\stop_bot.bat
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m playwright install chromium
.\старт\start_bot.bat
```

Linux с пользовательской службой:

```bash
systemctl --user stop avito-bot
cd "$HOME/avito-telegram-monitor"
git pull --ff-only
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m playwright install chromium
systemctl --user start avito-bot
```

Editable-установка (`-e`) гарантирует загрузку актуального `src`, а не старой
копии из `site-packages`. Для прежней установки без `-e` после обновления кода
переустановите пакет: `.venv/bin/python -m pip install --force-reinstall --no-deps .`.
Не запускайте одновременно старый и новый экземпляры.

## Резервное копирование и перенос

Остановите бот, затем сохраните в закрытую папку вне репозитория:

- `.env` (для старой системной установки — `/etc/avito-bot.env`);
- каталог `data` целиком, включая SQLite WAL/SHM, если они остались;
- `.runtime/avito-profile` при полностью закрытом Chromium.

Перенос базы сохраняет подписки, платежи и известные ID объявлений.
Профиль браузера может быть несовместим между ОС/версиями браузера — тогда
потребуется повторная ручная проверка. Не сохраняйте эти архивы в GitHub.

## Проверки и распространённые ошибки

Диагностика Telegram (не отправляет клиентам сообщений):

```powershell
.\.venv\Scripts\python.exe scripts/diagnose.py
```

На Linux замените путь интерпретатора на `.venv/bin/python`.

- **Токен отсутствует:** создайте `.env` и запускайте из корня проекта.
- **Unauthorized:** проверьте токен BotFather и отсутствие лишних пробелов.
- **Conflict/getUpdates:** где-то уже работает другой экземпляр с этим токеном
  или настроен webhook; остановите прежнюю установку. Диагностика показывает,
  установлен ли webhook; сама она его не удаляет.
- **Executable doesn't exist:** запустите установку Chromium тем же Python и
  пользователем, от которого работает бот.
- **Нет окна:** нужен `AVITO_HEADLESS=false` и активная графическая сессия;
  после входа импортируйте графическое окружение в user-systemd и перезапустите.
- **Профиль занят:** остановите бот до ручного открытия того же профиля.
- **429/капча:** требование пришло от Avito; повторы идут через выбранный
  интервал. Капчу пройдите в окне бота и используйте «Проверить сейчас» в `/list`.
- **Пауза 60 минут:** блокировки 429/капча не увеличивают интервал. Для прочих
  повторяющихся ошибок задержка до 60 минут предусмотрена. Смотрите `/status`.
- **Админ не выдался:** используйте личный чат, проверьте ID через `/id`,
  правильный файл окружения и перезапустите процесс; подробнее в [ADMIN.md](ADMIN.md).
- **Нет новых уведомлений:** первая выдача молчит намеренно; проверьте, что
  подписка активна, тариф действует, а в выдаче действительно появился новый ID.

Логи: `logs/bot.log`; для службы Linux также
`journalctl --user -u avito-bot -f`. Перед публикацией логов удалите личные ID,
ссылки, секреты и данные покупателей.

## Разработка и тесты

Установщики выше включают зависимости разработки. Тесты работают на временных
SQLite и HTML-фикстурах; реальный токен и платежи им не нужны.

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m pytest -q
```

В Windows используйте `.venv\Scripts\python.exe`. Не запускайте
`scripts/send_storefront_preview.py` и `deploy/debian/send_deploy_test.py`
без необходимости: эти отдельные утилиты отправляют сообщения в Telegram.
