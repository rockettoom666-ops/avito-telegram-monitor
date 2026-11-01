"""Загрузка настроек бота из переменных окружения и файла .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    """Все изменяемые настройки приложения в одном объекте.

    Секретный токен и параметры, которые могут отличаться на другом компьютере,
    не записываются в код: они читаются из .env методом ``load``.
    """

    bot_token: str
    database_path: Path
    log_path: Path
    avito_profile_dir: Path
    avito_headless: bool
    avito_max_items: int
    proxy_url: str
    avito_proxy_url: str
    max_subscriptions_per_user: int
    basic_plan_stars: int
    pro_plan_stars: int
    lifetime_plan_rubles: int
    lifetime_plan_stars: int
    support_contact: str
    merchant_name: str
    yookassa_provider_token: str
    yookassa_send_receipt: bool
    yookassa_vat_code: int
    yookassa_test_price_rubles: int
    admin_chat_ids: frozenset[int]

    @classmethod
    def load(cls) -> Settings:
        """Прочитать .env, проверить обязательные значения и собрать настройки."""

        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token or token == "replace_me":
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env "
                "and set the token."
            )

        # BotFather выдаёт отдельный платёжный токен после подключения ЮKassa.
        # В ранней версии проекта это поле ошибочно называлось SECRET_KEY.
        # Временный fallback позволяет не сломать уже настроенный локальный .env.
        provider_token = os.getenv("YOOKASSA_PROVIDER_TOKEN", "").strip()
        legacy_secret = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
        if not provider_token and ":" in legacy_secret:
            provider_token = legacy_secret

        return cls(
            bot_token=token,
            database_path=Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")),
            log_path=Path(os.getenv("LOG_PATH", "logs/bot.log")),
            avito_profile_dir=Path(
                os.getenv("AVITO_PROFILE_DIR", ".runtime/avito-profile")
            ),
            avito_headless=os.getenv("AVITO_HEADLESS", "false").lower()
            in {"1", "true", "yes"},
            avito_max_items=max(1, int(os.getenv("AVITO_MAX_ITEMS", "100"))),
            # PROXY_URL относится только к Telegram. Маршрут браузера задаётся
            # отдельно: пустой AVITO_PROXY_URL означает прямое соединение.
            proxy_url=os.getenv("PROXY_URL", "").strip(),
            avito_proxy_url=os.getenv("AVITO_PROXY_URL", "").strip(),
            max_subscriptions_per_user=max(
                1, int(os.getenv("MAX_SUBSCRIPTIONS_PER_USER", "10"))
            ),
            basic_plan_stars=max(1, int(os.getenv("BASIC_PLAN_STARS", "150"))),
            pro_plan_stars=max(1, int(os.getenv("PRO_PLAN_STARS", "250"))),
            lifetime_plan_rubles=max(
                1, int(os.getenv("LIFETIME_PLAN_RUBLES", "100"))
            ),
            lifetime_plan_stars=max(
                1, int(os.getenv("LIFETIME_PLAN_STARS", "75"))
            ),
            support_contact=os.getenv("SUPPORT_CONTACT", "@AvitoRacketka_bot").strip(),
            merchant_name=os.getenv(
                "MERCHANT_NAME", "Владелец @AvitoRacketka_bot"
            ).strip(),
            # Пустой provider token не мешает запуску остальных функций бота.
            yookassa_provider_token=provider_token,
            yookassa_send_receipt=os.getenv(
                "YOOKASSA_SEND_RECEIPT", "true"
            ).lower()
            in {"1", "true", "yes"},
            # Код НДС 1 означает «без НДС». При другой налоговой схеме владелец
            # магазина должен изменить значение в .env.
            yookassa_vat_code=max(
                1, min(6, int(os.getenv("YOOKASSA_VAT_CODE", "1")))
            ),
            # Telegram не принимает RUB-счета ниже своего min_amount. На июль
            # 2026 года лимит 87,73 ₽, поэтому ближайшая целая сумма — 88 ₽.
            yookassa_test_price_rubles=max(
                88, int(os.getenv("YOOKASSA_TEST_PRICE_RUBLES", "88"))
            ),
            # Тестовая кнопка видна только указанным администраторам. Это не
            # даёт обычным клиентам купить тариф по сниженной цене.
            admin_chat_ids=frozenset(
                int(item.strip())
                for item in os.getenv("ADMIN_CHAT_IDS", "").split(",")
                if item.strip()
            ),
        )

    @property
    def yookassa_configured(self) -> bool:
        """Получен ли через BotFather токен платёжного провайдера ЮKassa."""

        return bool(self.yookassa_provider_token)

    def ensure_directories(self) -> None:
        """Создать служебные каталоги до открытия БД, логов и профиля браузера."""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.avito_profile_dir.mkdir(parents=True, exist_ok=True)
