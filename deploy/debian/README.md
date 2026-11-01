# Развёртывание на Debian

> Это исторические примеры установки в `/opt/avito-bot` с Mihomo и пользователем
> `rockettoom`. Для чистой установки на другом ПК используйте
> [полный гайд Windows/Linux](../../docs/INSTALL.md) и
> [настройку администраторов](../../docs/ADMIN.md).
> Ниже приведены примеры конфигурации, а не описание текущего состояния ноутбука.
> Персональный `avito_proxy_rotation.py` с реквизитами прокси исключён из Git;
> шаблон без реквизитов находится в `avito_proxy_rotation.example.py`.

Файлы в этой папке предназначены для удалённого ноутбука. Секреты в репозиторий
не добавляются.

## Защищённый диск

Перед любыми изменениями нужно выполнить только читающие команды:

```bash
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL,SERIAL
findmnt -no SOURCE,TARGET,FSTYPE /
findmnt -no SOURCE,TARGET,FSTYPE /opt /var 2>/dev/null || true
```

Диск размером примерно 170 ГБ нельзя форматировать, размечать, монтировать,
очищать или использовать для проекта. Проект устанавливается в `/opt`, данные
служб — в `/var`, но только после проверки, на каком физическом диске находятся
эти каталоги.

## Схема сети

Mihomo слушает только `127.0.0.1:7890`. В `/etc/avito-bot.env` задаётся:

```dotenv
PROXY_URL=http://127.0.0.1:7890
AVITO_PROXY_URL=
AVITO_HEADLESS=false
```

Через прокси идёт только Telegram. Chromium и Avito работают напрямую через
обычный интернет-провайдер ноутбука. Системный маршрут и SSH не меняются.
Chromium запускается свёрнутым на физическом экране `:0`; если Avito показывает
капчу, бот автоматически разворачивает окно для ручной проверки.
Ссылка VPN-подписки записывается только в `/etc/mihomo/config.yaml` с правами
`640` и группой `mihomo`.

## Службы

- `mihomo.service` — локальный VPN-прокси;
- `avito-bot.service` — бот с автоматическим перезапуском.

Для ручного подключения или замены отдельного прокси Avito используйте
`set_avito_proxy.sh`. Инструкция находится в `docs/AVITO_PROXY_MANUAL.md`.

После переноса базы и проверки:

```bash
sudo systemctl enable --now mihomo.service
sudo systemctl enable --now avito-bot.service
sudo systemctl status mihomo.service avito-bot.service --no-pager
sudo journalctl -u avito-bot.service -n 100 --no-pager
```

## Ручная проверка Avito

Никакой отдельный ярлык или VNC не нужен. При обнаружении страницы проверки
Chromium автоматически переходит из свёрнутого состояния в развёрнутое на
экране Debian-ноутбука. После решения капчи окно можно снова свернуть.
