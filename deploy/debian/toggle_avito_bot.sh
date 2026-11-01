#!/usr/bin/env bash
# Запустить или полностью остановить Telegram-бота одним ярлыком.

set -u

bot_service="avito-bot.service"
vpn_service="mihomo.service"
systemctl_path="/usr/bin/systemctl"

show_result_and_wait() {
  echo
  read -r -p "Нажмите Enter, чтобы закрыть окно..."
}

if "${systemctl_path}" is-active --quiet "${bot_service}"; then
  echo "Останавливаю Telegram-бота..."

  # Сначала останавливается бот, чтобы он штатно закрыл БД и Chromium.
  if ! sudo -n "${systemctl_path}" stop "${bot_service}"; then
    echo "Ошибка: не удалось остановить ${bot_service}."
    show_result_and_wait
    exit 1
  fi

  # Mihomo запускается как зависимость бота. После остановки бота отдельный
  # прокси-процесс больше не нужен.
  if ! sudo -n "${systemctl_path}" stop "${vpn_service}"; then
    echo "Предупреждение: не удалось остановить ${vpn_service}."
  fi

  echo "Бот и его VPN-прокси остановлены."
  show_result_and_wait
  exit 0
fi

echo "Запускаю Telegram-бота..."

# avito-bot.service содержит Requires=mihomo.service, поэтому одной командой
# systemd сначала поднимет Mihomo, а затем основной процесс Telegram-бота.
if ! sudo -n "${systemctl_path}" start "${bot_service}"; then
  echo "Ошибка запуска. Последние строки журнала:"
  journalctl -u "${bot_service}" -n 15 --no-pager
  show_result_and_wait
  exit 1
fi

sleep 3
if "${systemctl_path}" is-active --quiet "${bot_service}"; then
  echo "Бот успешно запущен."
else
  echo "Служба запустилась, но сразу завершилась. Последние строки журнала:"
  journalctl -u "${bot_service}" -n 15 --no-pager
  show_result_and_wait
  exit 1
fi

show_result_and_wait
