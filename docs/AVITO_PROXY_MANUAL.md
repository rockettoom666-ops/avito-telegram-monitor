# Ручное переключение прокси Avito

Telegram и Avito используют разные сетевые настройки:

- `PROXY_URL` — только соединение Telegram Bot API;
- `AVITO_PROXY_URL` — только Chromium, который открывает Avito.

Скрипт `deploy/debian/set_avito_proxy.sh` безопасно изменяет только
`AVITO_PROXY_URL`, сохраняет предыдущий файл настроек в
`/etc/avito-bot.env.proxy-backup` и перезапускает бот.

## Команды на ноутбуке

Включить заданный вручную HTTP-прокси:

```bash
sudo /opt/avito-bot/deploy/debian/set_avito_proxy.sh \
  'http://login:password@host:port'
```

Поддерживаются также `https://`, `socks5://` и `socks5h://`.

Вернуть прямое соединение:

```bash
sudo /opt/avito-bot/deploy/debian/set_avito_proxy.sh direct
```

Проверить службы:

```bash
systemctl is-active mihomo.service avito-bot.service
```

Адрес с логином и паролем является секретом. Его нельзя публиковать, добавлять
в Git или передавать в журналы. В чистой установке автоматический таймер
ротации не создаётся.

## Python-файл со списком прокси

Персональный файл со списком прокси исключён из Git. Для существующего
развёртывания в `/opt` можно скопировать `avito_proxy_rotation.example.py`
рядом под именем `avito_proxy_rotation.py` и заполнить `PROXY_URLS` локально.
Не заменяйте существующий рабочий файл шаблоном. Он относится к старой схеме
цепочки с `avito-proxy-chain.service` и `mihomo.service`, а не к новой
пользовательской службе из INSTALL.md.

Список адресов можно заполнить в файле:

```text
/opt/avito-bot/deploy/debian/avito_proxy_rotation.py
```

Нужное место находится в начале файла:

```python
PROXY_URLS = (
    "http://login:password@host1:port",
    "http://login:password@host2:port",
)
```

Один ручной запуск переключает Avito на следующий адрес:

```bash
sudo /opt/avito-bot/deploy/debian/avito_proxy_rotation.py
```

Цепочка из этого шаблона рассчитана на HTTP-прокси: мост слушает
`127.0.0.1:7892`, а Mihomo — `127.0.0.1:7890`. Для неё дополнительно
нужен установщик `configure_avito_proxy_chain.py`; он содержит имя
пользователя `rockettoom`, которое надо адаптировать к своей машине.
После изменения списка установщик заново создаёт защищённый системный JSON.
Обычная конфигурация `AVITO_PROXY_URL` может использоваться без этой цепочки.

Вернуть прямое соединение:

```bash
sudo /opt/avito-bot/deploy/debian/avito_proxy_rotation.py --direct
```
