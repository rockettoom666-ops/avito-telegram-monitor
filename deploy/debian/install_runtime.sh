#!/usr/bin/env bash
set -euo pipefail

# Этот скрипт не размечает, не форматирует и не монтирует диски. Он работает
# только с системными каталогами Debian. Перед запуском мы отдельно сверяем
# вывод lsblk/findmnt и убеждаемся, что защищённый диск ~170 ГБ не используется.

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите через sudo: sudo bash deploy/debian/install_runtime.sh" >&2
  exit 1
fi

project_dir="/opt/avito-bot"
proxy_url="http://127.0.0.1:7890"
python_install_dir="${project_dir}/.python"
python_binary="${python_install_dir}/cpython-3.12-linux-x86_64-gnu/bin/python3.12"

if [[ ! -f "${project_dir}/pyproject.toml" ]]; then
  echo "Сначала перенесите проект в ${project_dir}." >&2
  exit 1
fi

if [[ ! -f "/etc/avito-bot.env" ]]; then
  echo "Не найден защищённый файл /etc/avito-bot.env." >&2
  exit 1
fi

if ! systemctl is-active --quiet mihomo.service; then
  echo "Сначала настройте и запустите mihomo.service." >&2
  exit 1
fi

# Подтверждаем, что локальный прокси действительно выпускает HTTPS-трафик.
curl --fail --silent --show-error \
  --proxy "${proxy_url}" \
  --max-time 20 \
  https://api.telegram.org >/dev/null

export HTTP_PROXY="${proxy_url}"
export HTTPS_PROXY="${proxy_url}"
export ALL_PROXY="${proxy_url}"
# Часть Linux-инструментов (включая apt) читает только имена в нижнем
# регистре, поэтому задаём оба варианта.
export http_proxy="${proxy_url}"
export https_proxy="${proxy_url}"
export all_proxy="${proxy_url}"
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="${NO_PROXY}"
export UV_HTTP_TIMEOUT=120
export PLAYWRIGHT_BROWSERS_PATH="${project_dir}/.playwright"

# uv ставит требуемый Python 3.12 независимо от версии Python в Debian.
if [[ ! -x /usr/local/bin/uv ]]; then
  curl --fail --location --silent --show-error \
    https://astral.sh/uv/install.sh |
    env UV_INSTALL_DIR=/usr/local/bin sh
fi

id -u avito-bot >/dev/null 2>&1 ||
  useradd --system --home-dir /var/lib/avito-bot \
    --create-home --shell /usr/sbin/nologin avito-bot

install -d -o avito-bot -g avito-bot \
  "${project_dir}/data" \
  "${project_dir}/logs" \
  "${project_dir}/.runtime/avito-profile" \
  "${project_dir}/.playwright" \
  /var/lib/avito-bot

# Python нельзя оставлять в /root: виртуальное окружение создаёт ссылку на
# интерпретатор, а отдельная служба avito-bot не имеет доступа к домашней папке
# root. Поэтому самостоятельный Python хранится рядом с приложением.
/usr/local/bin/uv python install \
  --install-dir "${python_install_dir}" \
  --no-bin \
  3.12
/usr/local/bin/uv venv \
  --clear \
  --python "${python_binary}" \
  "${project_dir}/.venv"
/usr/local/bin/uv pip install --python "${project_dir}/.venv/bin/python" \
  "${project_dir}[dev]"

# Playwright устанавливает Chromium и его системные библиотеки. Переменные
# прокси выше применяются и к загрузке браузера, и к apt внутри install-deps.
"${project_dir}/.venv/bin/python" -m playwright install --with-deps chromium

chown -R avito-bot:avito-bot \
  "${project_dir}/data" \
  "${project_dir}/logs" \
  "${project_dir}/.runtime" \
  "${project_dir}/.playwright"
# Интерпретатор и библиотеки остаются владельцем root, но служба должна иметь
# право их читать и запускать. Секреты, база и профиль этим не открываются.
chmod -R a+rX "${python_install_dir}" "${project_dir}/.venv"
chmod 600 /etc/avito-bot.env

install -m 0644 \
  "${project_dir}/deploy/debian/avito-bot.service" \
  /etc/systemd/system/avito-bot.service
systemctl daemon-reload
systemctl enable avito-bot.service

echo "Среда готова. Перед запуском перенесите базу и выполните тесты."
