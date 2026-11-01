#!/usr/bin/env bash
# Ручное переключение прокси только для браузера Avito.
# Telegram использует отдельную переменную PROXY_URL и этим скриптом не меняется.

set -euo pipefail

env_file="/etc/avito-bot.env"
service_name="avito-bot.service"

show_usage() {
  cat <<'EOF'
Использование:
  sudo ./set_avito_proxy.sh direct
  sudo ./set_avito_proxy.sh 'http://login:password@host:port'
  sudo ./set_avito_proxy.sh 'socks5://login:password@host:port'

Команда direct отключает прокси для Avito.
EOF
}

if [[ ${EUID} -ne 0 ]]; then
  echo "Запустите скрипт через sudo." >&2
  exit 1
fi

if [[ $# -ne 1 ]]; then
  show_usage >&2
  exit 2
fi

if [[ ! -f "${env_file}" ]]; then
  echo "Не найден ${env_file}." >&2
  exit 1
fi

requested_proxy="$1"
if [[ "${requested_proxy}" == "direct" ]]; then
  proxy_url=""
else
  case "${requested_proxy}" in
    http://*|https://*|socks5://*|socks5h://*) proxy_url="${requested_proxy}" ;;
    *)
      echo "Допустимы http://, https://, socks5://, socks5h:// или direct." >&2
      exit 2
      ;;
  esac
fi

# Перевод строки позволил бы повредить другие параметры .env.
if [[ "${proxy_url}" == *$'\n'* || "${proxy_url}" == *$'\r'* ]]; then
  echo "Адрес прокси содержит недопустимые символы." >&2
  exit 2
fi

umask 077
temporary_file="$(mktemp "/etc/avito-bot.env.proxy.XXXXXX")"
trap 'rm -f "${temporary_file}"' EXIT

# Меняется только AVITO_PROXY_URL. Токен Telegram и остальные секреты
# переписываются без изменений и никогда не выводятся в терминал.
awk -v value="${proxy_url}" '
  BEGIN { found = 0 }
  /^AVITO_PROXY_URL=/ {
    print "AVITO_PROXY_URL=" value
    found = 1
    next
  }
  { print }
  END {
    if (!found) print "AVITO_PROXY_URL=" value
  }
' "${env_file}" >"${temporary_file}"

cp -a "${env_file}" "/etc/avito-bot.env.proxy-backup"
chown --reference="${env_file}" "${temporary_file}"
chmod --reference="${env_file}" "${temporary_file}"
mv -f "${temporary_file}" "${env_file}"
trap - EXIT

# Playwright читает прокси при запуске Chromium, поэтому после изменения
# конфигурации требуется полный перезапуск службы.
systemctl restart "${service_name}"
systemctl is-active --quiet "${service_name}"

if [[ -n "${proxy_url}" ]]; then
  echo "Прокси Avito установлен; ${service_name} работает."
else
  echo "Avito переключён на прямое соединение; ${service_name} работает."
fi
