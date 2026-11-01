#!/usr/bin/env bash
set -euo pipefail

# Устанавливает только локальный прокси без TUN и без изменения таблицы
# маршрутизации. Поэтому активное SSH-соединение продолжает работать напрямую.

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите скрипт через sudo." >&2
  exit 1
fi

subscription_file="${VPN_SUBSCRIPTION_FILE:-/tmp/vpn-subscription-url}"
if [[ ! -f "${subscription_file}" ]]; then
  echo "Не найден временный файл со ссылкой VPN: ${subscription_file}" >&2
  exit 1
fi

for command_name in curl jq gzip install systemctl; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Перед установкой нужен пакет с командой: ${command_name}" >&2
    exit 1
  fi
done

case "$(uname -m)" in
  x86_64)
    asset_pattern='^mihomo-linux-amd64-compatible-.*[.]gz$'
    ;;
  aarch64 | arm64)
    asset_pattern='^mihomo-linux-arm64-v8-.*[.]gz$'
    ;;
  *)
    echo "Неподдерживаемая архитектура: $(uname -m)" >&2
    exit 1
    ;;
esac

release_json="$(mktemp)"
archive_file="$(mktemp --suffix=.gz)"
binary_file="$(mktemp)"
cleanup() {
  rm -f -- "${release_json}" "${archive_file}" "${binary_file}"
}
trap cleanup EXIT

curl --fail --location --silent --show-error \
  https://api.github.com/repos/MetaCubeX/mihomo/releases/latest \
  -o "${release_json}"
download_url="$(
  jq -r --arg pattern "${asset_pattern}" \
    '.assets[] | select(.name | test($pattern)) | .browser_download_url' \
    "${release_json}" |
    head -n 1
)"
if [[ -z "${download_url}" || "${download_url}" == "null" ]]; then
  echo "В последнем релизе Mihomo не найден файл для этой архитектуры." >&2
  exit 1
fi

curl --fail --location --silent --show-error \
  "${download_url}" \
  -o "${archive_file}"
gzip -dc "${archive_file}" >"${binary_file}"
install -m 0755 "${binary_file}" /usr/local/bin/mihomo

id -u mihomo >/dev/null 2>&1 ||
  useradd --system --home-dir /var/lib/mihomo \
    --create-home --shell /usr/sbin/nologin mihomo
install -d -o root -g mihomo -m 0750 /etc/mihomo
install -d -o mihomo -g mihomo -m 0750 /var/lib/mihomo

subscription_url="$(head -n 1 "${subscription_file}" | tr -d '\r\n')"
if [[ "${subscription_url}" != https://* ]]; then
  echo "VPN-подписка должна начинаться с https://." >&2
  exit 1
fi
escaped_url="$(
  printf '%s' "${subscription_url}" |
    sed -e 's/[\\&|]/\\&/g' -e 's/"/\\"/g'
)"
sed "s|__VPN_SUBSCRIPTION_URL__|${escaped_url}|" \
  /opt/avito-bot/deploy/debian/mihomo-config.yaml.template \
  >/etc/mihomo/config.yaml
chown root:mihomo /etc/mihomo/config.yaml
chmod 0640 /etc/mihomo/config.yaml

install -m 0644 \
  /opt/avito-bot/deploy/debian/mihomo.service \
  /etc/systemd/system/mihomo.service
/usr/local/bin/mihomo -t -d /var/lib/mihomo -f /etc/mihomo/config.yaml
systemctl daemon-reload
systemctl enable --now mihomo.service

# Временный файл с ключом больше не нужен.
rm -f -- "${subscription_file}"
echo "Mihomo установлен и слушает только 127.0.0.1:7890."
