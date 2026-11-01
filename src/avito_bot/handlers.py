"""Команды Telegram, кнопки, создание поисков и обработка платежей."""

from __future__ import annotations

import html
import json
import logging
from urllib.parse import parse_qs, unquote_plus, urlsplit

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from avito_bot.config import Settings
from avito_bot.db import Database
from avito_bot.keyboards import (
    interval_keyboard,
    payment_confirmation_keyboard,
    plans_keyboard,
    subscription_interval_keyboard,
    subscription_keyboard,
)
from avito_bot.models import AccessStatus
from avito_bot.monitor import MonitorService
from avito_bot.plans import (
    SubscriptionPlan,
    format_search_count,
    get_plans,
    plan_period_text,
)
from avito_bot.utils import (
    ALLOWED_INTERVALS,
    format_dt,
    normalize_avito_url,
)

logger = logging.getLogger(__name__)


HELP_TEXT = (
    "Отправьте мне ссылку на страницу поиска Avito, затем выберите интервал. "
    "Текущие объявления я запомню молча и сообщу только о тех, которые "
    "появятся позже.\n\n"
    "Команды:\n"
    "/id — мой Telegram ID\n"
    "/list — мои подписки\n"
    "/status — состояние проверок\n"
    "/subscription — подписка и тарифы\n"
    "/buy — купить или продлить подписку\n"
    "/faq — частые вопросы\n"
    "/terms — условия использования\n"
    "/paysupport — помощь с оплатой\n"
    "/help — эта справка"
)

FAQ_TEXT = (
    "<b>❓ Частые вопросы</b>\n\n"
    "<b>Что означает количество поисков в тарифе?</b>\n"
    "Один поиск — это одна сохранённая ссылка на выдачу Avito со своими "
    "настройками: товаром, городом, ценой и фильтрами.\n\n"
    "Например, тариф на 2 поиска позволяет одновременно запустить:\n"
    "1. Поиск MacBook в Нижнем Новгороде.\n"
    "2. Поиск iPhone в Москве.\n\n"
    "Тариф на 5 поисков позволяет боту одновременно следить за пятью разными "
    "ссылками Avito. Каждый поиск работает независимо и может иметь свой "
    "интервал проверки.\n\n"
    "<b>Бот пришлёт все старые объявления?</b>\n"
    "Нет. При первом запуске бот запоминает текущую выдачу без уведомлений, "
    "а затем присылает только новые объявления.\n\n"
    "<b>Можно изменить или остановить поиск?</b>\n"
    "Да. Откройте /list — там можно проверить поиск сейчас, изменить интервал, "
    "приостановить, возобновить или удалить его.\n\n"
    "<b>Что будет после окончания подписки?</b>\n"
    "Проверки приостановятся. Сохранённые поиски останутся в боте и продолжат "
    "работу после продления подписки через /buy. Тариф «Навсегда» не "
    "заканчивается: он включает один поиск с интервалом от 15 минут."
)


class AddSubscription(StatesGroup):
    """Шаги диалога добавления нового поиска."""

    # После получения URL бот временно ждёт нажатия кнопки с интервалом.
    choosing_interval = State()


def _search_name(url: str) -> str:
    """Получить короткое понятное имя поиска из параметра q или пути URL."""

    parts = urlsplit(url)
    query = parse_qs(parts.query)
    phrase = unquote_plus(query.get("q", [""])[0]).strip()
    if phrase:
        return phrase[:80]
    path_name = parts.path.rstrip("/").split("/")[-1].replace("_", " ")
    return path_name[:80] or "Поиск Avito"


def _access_text(access: AccessStatus, plans: dict[str, SubscriptionPlan]) -> str:
    """Сформировать пользовательское описание пробного или платного доступа."""

    if access.active and access.plan_code == "admin":
        return (
            "🛡 <b>Администратор: вечный доступ</b>\n"
            f"Доступно поисков: {access.search_limit}\n"
            "Минимальный интервал: 2 минуты\n"
            "Срок действия: бессрочно"
        )
    if access.active and access.is_trial:
        return (
            "🎁 <b>Пробный период активен</b>\n"
            f"Доступно поисков: {access.search_limit}\n"
            f"До: {format_dt(access.access_until)}"
        )
    if access.active and access.is_lifetime:
        return (
            "♾ <b>Тариф «Навсегда» активен</b>\n"
            "Доступно поисков: 1\n"
            "Минимальный интервал: 15 минут\n"
            "Срок действия: бессрочно"
        )
    if access.active:
        plan = plans.get(access.plan_code or "")
        plan_name = plan.title if plan else (access.plan_code or "Подписка")
        return (
            f"✅ <b>Тариф «{html.escape(plan_name)}» активен</b>\n"
            f"Доступно поисков: {access.search_limit}\n"
            f"До: {format_dt(access.access_until)}"
        )
    return (
        "⛔ <b>Подписка не активна</b>\n"
        "Мониторинг приостановлен. Выберите тариф для продолжения."
    )


def _invoice_payload(
    plan_code: str,
    chat_id: int,
    provider: str = "telegram_stars",
) -> str:
    """Вложить способ оплаты, тариф и владельца в служебную строку счёта."""

    # Старый формат оставлен для Stars: уже отправленные счета продолжат
    # корректно обрабатываться после обновления бота.
    if provider == "telegram_stars":
        return f"subscription:v1:{plan_code}:{chat_id}"
    if provider == "yookassa":
        return f"subscription:yookassa:v1:{plan_code}:{chat_id}"
    if provider == "yookassa_test":
        return f"subscription:yookassa_test:v1:{plan_code}:{chat_id}"
    raise ValueError(f"Unsupported payment provider: {provider}")


def _payload_details(payload: str, chat_id: int) -> tuple[str, str] | None:
    """Извлечь способ оплаты и тариф только из счёта нужного пользователя."""

    parts = payload.split(":")
    if (
        len(parts) == 4
        and parts[:2] == ["subscription", "v1"]
        and parts[3] == str(chat_id)
    ):
        return "telegram_stars", parts[2]
    if (
        len(parts) == 5
        and parts[0] == "subscription"
        and parts[1] in {"yookassa", "yookassa_test"}
        and parts[2] == "v1"
        and parts[4] == str(chat_id)
    ):
        return parts[1], parts[3]
    return None


def _payload_plan(payload: str, chat_id: int) -> str | None:
    """Вернуть тариф из валидного payload (совместимость со старыми тестами)."""

    details = _payload_details(payload, chat_id)
    return details[1] if details else None


def _payment_matches(
    provider: str,
    plan: SubscriptionPlan | None,
    currency: str,
    total_amount: int,
    *,
    allow_admin_test: bool,
    test_price_rubles: int = 88,
) -> bool:
    """Проверить валюту, сумму и доступ к сниженному тестовому счёту."""

    if plan is None:
        return False
    if provider == "telegram_stars":
        return currency == "XTR" and total_amount == plan.stars_price
    if provider == "yookassa":
        return currency == "RUB" and total_amount == plan.rub_price * 100
    if provider == "yookassa_test":
        return (
            allow_admin_test
            and currency == "RUB"
            and total_amount == test_price_rubles * 100
        )
    return False


async def send_subscription_invoice(
    bot: Bot,
    chat_id: int,
    plan: SubscriptionPlan,
) -> Message:
    """Отправить встроенный счёт Telegram Stars методом ``sendInvoice``."""

    return await bot.send_invoice(
        chat_id=chat_id,
        title=f"Подписка «{plan.title}»",
        description=(
            f"{format_search_count(plan.search_limit)} объявлений Avito "
            f"{plan_period_text(plan)}"
        ),
        payload=_invoice_payload(plan.code, chat_id),
        provider_token="",
        currency="XTR",
        prices=[
            LabeledPrice(
                label=f"Тариф «{plan.title}», {plan_period_text(plan)}",
                amount=plan.stars_price,
            )
        ],
        start_parameter=f"subscribe_{plan.code}",
    )


async def send_yookassa_invoice(
    bot: Bot,
    chat_id: int,
    plan: SubscriptionPlan,
    provider_token: str,
    *,
    send_receipt: bool = True,
    vat_code: int = 1,
    test_payment: bool = False,
    test_price_rubles: int = 88,
) -> Message:
    """Отправить рублёвый Telegram-счёт через подключённую ЮKassa.

    Если используются «Чеки от ЮKassa», провайдеру нужны e-mail покупателя и
    JSON с позицией чека. Без этих полей ЮKassa может отклонить оплату.
    """

    # Тестовая кнопка использует тот же настоящий платёжный сценарий. Сумма
    # не может быть 1 ₽: Telegram сам ограничивает минимальный RUB-платёж.
    rubles = test_price_rubles if test_payment else plan.rub_price
    provider = "yookassa_test" if test_payment else "yookassa"
    invoice_title = (
        f"Тест подписки «{plan.title}»"
        if test_payment
        else f"Подписка «{plan.title}»"
    )
    receipt_description = (
        f"Тест подписки «{plan.title}»"
        if test_payment
        else f"Подписка «{plan.title}» {plan_period_text(plan)}"
    )

    invoice_options: dict[str, object] = {}
    if send_receipt:
        receipt = {
            "receipt": {
                "items": [
                    {
                        "description": receipt_description,
                        "quantity": "1.00",
                        "amount": {
                            "value": f"{rubles:.2f}",
                            "currency": "RUB",
                        },
                        "vat_code": vat_code,
                        "payment_mode": "full_payment",
                        "payment_subject": "service",
                    }
                ]
            }
        }
        invoice_options.update(
            need_email=True,
            send_email_to_provider=True,
            provider_data=json.dumps(receipt, ensure_ascii=False),
        )

    return await bot.send_invoice(
        chat_id=chat_id,
        title=invoice_title,
        description=(
            f"Проверка оплаты и автоматической активации за {rubles} рублей"
            if test_payment
            else (
                f"{format_search_count(plan.search_limit)} объявлений Avito "
                f"{plan_period_text(plan)}"
            )
        ),
        payload=_invoice_payload(plan.code, chat_id, provider),
        provider_token=provider_token,
        currency="RUB",
        # Для RUB Telegram принимает цену в копейках, поэтому умножаем на 100.
        prices=[
            LabeledPrice(
                label=(
                    f"Тест тарифа «{plan.title}»"
                    if test_payment
                    else f"Тариф «{plan.title}», {plan_period_text(plan)}"
                ),
                amount=rubles * 100,
            )
        ],
        start_parameter=(
            f"yookassa_test_{plan.code}"
            if test_payment
            else f"yookassa_{plan.code}"
        ),
        **invoice_options,
    )


def create_router(db: Database, settings: Settings, monitor: MonitorService) -> Router:
    """Создать aiogram Router и зарегистрировать в нём все обработчики бота."""

    router = Router(name="avito-bot")
    plans = get_plans(settings)

    def terms_text() -> str:
        """Собрать условия с контактами владельца из настроек."""

        return (
            "<b>Условия использования</b>\n\n"
            "Сервис предоставляет автоматический мониторинг объявлений Avito. "
            "Пробный период длится 24 часа и включает один поиск.\n\n"
            "Тарифы «Старт» и «Про» действуют 7 суток. Тариф «Навсегда» "
            "предоставляет бессрочный доступ к одному поиску с минимальным "
            "интервалом 15 минут. Автоматического продления нет. Доступность и "
            "скорость уведомлений могут зависеть от Avito, интернета и "
            "антибот-защиты.\n\n"
            "Нажимая «Принять условия и оплатить», пользователь соглашается с "
            "этими условиями. По вопросам платежей и возвратов: "
            "@RaketManager"
        )

    # ------------------------------------------------------------------
    # Основные команды и экраны подписки
    # ------------------------------------------------------------------

    @router.message(Command("start"))
    async def start_handler(message: Message, state: FSMContext) -> None:
        """Начать чистый диалог и при первом запуске активировать trial."""

        await state.clear()
        access = await db.ensure_user(message.chat.id)
        logger.info("User %s started the bot", message.chat.id)
        await message.answer(
            "👋 Я слежу за поисками на Avito и присылаю только новые объявления.\n\n"
            + _access_text(access, plans)
            + "\n\n"
            + HELP_TEXT,
            parse_mode=ParseMode.HTML,
        )

    @router.message(Command("help"))
    async def help_handler(message: Message) -> None:
        """Показать короткую инструкцию и список команд."""

        await message.answer(HELP_TEXT)

    @router.message(Command("id"))
    async def id_handler(message: Message) -> None:
        """Показать ID для настройки ADMIN_CHAT_IDS в личном чате с ботом."""

        if message.from_user is None:
            return
        await message.answer(
            f"Ваш Telegram ID: {message.from_user.id}\n"
            "Для админ-доступа владелец добавляет этот номер в ADMIN_CHAT_IDS "
            "в файле .env и перезапускает бота. Используйте бота в личном чате."
        )

    @router.message(Command("faq"))
    async def faq_handler(message: Message) -> None:
        """Ответить на частые вопросы о поисках и подписке."""

        await message.answer(FAQ_TEXT, parse_mode=ParseMode.HTML)

    @router.message(Command("subscription"))
    async def subscription_handler(message: Message) -> None:
        """Показать текущий доступ и кнопки тарифов."""

        access = await db.get_access(message.chat.id)
        await message.answer(
            _access_text(access, plans),
            parse_mode=ParseMode.HTML,
            reply_markup=plans_keyboard(plans),
        )

    @router.message(Command("buy"))
    async def buy_handler(message: Message) -> None:
        """Открыть витрину платных тарифов."""

        await db.ensure_user(message.chat.id)
        await message.answer(
            "<b>Тарифы</b>\n\n"
            "«Старт» — 2 поиска, 200 ₽.\n"
            "«Про» — 5 поисков, 350 ₽.\n"
            "«Навсегда» — 1 поиск, бессрочно, от 15 минут, 100 ₽.\n\n"
            "Количество Stars указано на кнопках.",
            parse_mode=ParseMode.HTML,
            reply_markup=plans_keyboard(plans),
        )

    @router.message(Command("terms"))
    async def terms_handler(message: Message) -> None:
        """Показать условия использования отдельной командой."""

        await message.answer(terms_text(), parse_mode=ParseMode.HTML)

    @router.message(Command("paysupport"))
    async def payment_support_handler(message: Message) -> None:
        """Сообщить контакт для вопросов об оплате и возврате."""

        await message.answer(
            "По вопросам оплаты и возвратов напишите: @RaketManager"


        )

    # ------------------------------------------------------------------
    # Покупка подписки: Telegram Stars или встроенный счёт ЮKassa
    # ------------------------------------------------------------------

    @router.callback_query(F.data.startswith("buy:"))
    async def choose_plan_handler(callback: CallbackQuery) -> None:
        """Показать детали выбранного тарифа до создания счёта."""

        if callback.message is None:
            return
        plan_code = (callback.data or "").split(":", 1)[-1]
        plan = plans.get(plan_code)
        if plan is None:
            await callback.answer("Тариф не найден", show_alert=True)
            return
        await callback.message.answer(
            f"<b>Тариф «{html.escape(plan.title)}»</b>\n"
            f"{format_search_count(plan.search_limit)}, "
            f"{plan_period_text(plan)}.\n"
            f"Минимальный интервал: {plan.min_interval_minutes} мин.\n"
            f"Стоимость: {plan.rub_price} ₽ или {plan.stars_price} ⭐.\n\n"
            "Выберите способ оплаты. Автоматического продления нет.",
            parse_mode=ParseMode.HTML,
            reply_markup=payment_confirmation_keyboard(
                plan,
                show_test_payment=callback.from_user.id
                in settings.admin_chat_ids,
                test_payment_rubles=settings.yookassa_test_price_rubles,
            ),
        )
        await callback.answer()

    @router.callback_query(F.data == "terms:show")
    async def terms_callback_handler(callback: CallbackQuery) -> None:
        """Открыть условия по inline-кнопке под карточкой тарифа."""

        if callback.message is not None:
            await callback.message.answer(terms_text(), parse_mode=ParseMode.HTML)
        await callback.answer()

    @router.callback_query(F.data.startswith("pay:stars:"))
    async def pay_plan_handler(callback: CallbackQuery) -> None:
        """Создать встроенный счёт Telegram в валюте XTR (Stars)."""

        if callback.message is None:
            return
        plan_code = (callback.data or "").split(":", 2)[-1]
        plan = plans.get(plan_code)
        if plan is None:
            await callback.answer("Тариф не найден", show_alert=True)
            return
        await send_subscription_invoice(
            callback.bot,
            callback.message.chat.id,
            plan,
        )
        await callback.answer()

    @router.callback_query(F.data.in_({"pay:basic", "pay:pro"}))
    async def legacy_pay_plan_handler(callback: CallbackQuery) -> None:
        """Поддержать старые кнопки Stars из уже отправленных сообщений."""

        if callback.message is None:
            return
        plan_code = (callback.data or "").split(":", 1)[-1]
        plan = plans.get(plan_code)
        if plan is None:
            await callback.answer("Тариф не найден", show_alert=True)
            return
        await send_subscription_invoice(callback.bot, callback.message.chat.id, plan)
        await callback.answer()

    @router.callback_query(F.data.startswith("pay:yookassa:"))
    async def yookassa_plan_handler(callback: CallbackQuery) -> None:
        """Выставить рублёвый Telegram-счёт через провайдера ЮKassa."""

        if callback.message is None:
            return
        plan_code = (callback.data or "").split(":", 2)[-1]
        plan = plans.get(plan_code)
        if plan is None:
            await callback.answer("Тариф не найден", show_alert=True)
            return
        if not settings.yookassa_configured:
            await callback.answer(
                "Добавьте YOOKASSA_PROVIDER_TOKEN из BotFather в файл .env.",
                show_alert=True,
            )
            return
        await callback.answer("Создаю счёт…")
        try:
            await send_yookassa_invoice(
                callback.bot,
                callback.message.chat.id,
                plan,
                settings.yookassa_provider_token,
                send_receipt=settings.yookassa_send_receipt,
                vat_code=settings.yookassa_vat_code,
            )
        except TelegramAPIError as error:
            logger.warning("Could not send YooKassa invoice: %s", error)
            await callback.message.answer(
                "Не удалось выставить счёт ЮKassa. Проверьте подключение "
                "бота в BotFather → Payments и настройки чеков."
            )

    @router.callback_query(F.data.startswith("pay:yookassa_test:"))
    async def yookassa_test_plan_handler(callback: CallbackQuery) -> None:
        """Выставить админу минимальный RUB-счёт для проверки полного цикла."""

        if callback.message is None:
            return
        if callback.from_user.id not in settings.admin_chat_ids:
            logger.warning(
                "User %s tried to use admin test payment",
                callback.from_user.id,
            )
            await callback.answer("Эта кнопка доступна только админу", show_alert=True)
            return
        plan_code = (callback.data or "").split(":", 2)[-1]
        plan = plans.get(plan_code)
        if plan is None:
            await callback.answer("Тариф не найден", show_alert=True)
            return
        if not settings.yookassa_configured:
            await callback.answer(
                "Добавьте YOOKASSA_PROVIDER_TOKEN из BotFather в файл .env.",
                show_alert=True,
            )
            return
        await callback.answer(
            f"Создаю тестовый счёт на {settings.yookassa_test_price_rubles} ₽…"
        )
        try:
            await send_yookassa_invoice(
                callback.bot,
                callback.message.chat.id,
                plan,
                settings.yookassa_provider_token,
                send_receipt=settings.yookassa_send_receipt,
                vat_code=settings.yookassa_vat_code,
                test_payment=True,
                test_price_rubles=settings.yookassa_test_price_rubles,
            )
        except TelegramAPIError as error:
            logger.warning("Could not send YooKassa test invoice: %s", error)
            await callback.message.answer(
                "Не удалось выставить тестовый счёт ЮKassa. Проверьте "
                "provider token и настройки чеков."
            )

    @router.pre_checkout_query()
    async def pre_checkout_handler(query: PreCheckoutQuery) -> None:
        """Перед оплатой ещё раз сверить владельца, валюту и сумму счёта."""

        details = _payload_details(query.invoice_payload, query.from_user.id)
        provider, plan_code = details or ("", "")
        plan = plans.get(plan_code or "")
        # На pre_checkout_query нужно ответить быстрее 10 секунд. Поэтому здесь
        # только быстрая локальная проверка — без обращения к сторонним API.
        valid = _payment_matches(
            provider,
            plan,
            query.currency,
            query.total_amount,
            allow_admin_test=query.from_user.id in settings.admin_chat_ids,
            test_price_rubles=settings.yookassa_test_price_rubles,
        )
        await query.answer(
            ok=valid,
            error_message=None
            if valid
            else "Тариф или стоимость изменились. Создайте новый счёт через /buy.",
        )

    @router.message(F.successful_payment)
    async def successful_payment_handler(message: Message) -> None:
        """Учесть подтверждённый Telegram платёж и включить новый лимит."""

        payment = message.successful_payment
        if payment is None:
            return
        details = _payload_details(payment.invoice_payload, message.chat.id)
        provider, plan_code = details or ("", "")
        plan = plans.get(plan_code or "")
        valid = _payment_matches(
            provider,
            plan,
            payment.currency,
            payment.total_amount,
            allow_admin_test=message.chat.id in settings.admin_chat_ids,
            test_price_rubles=settings.yookassa_test_price_rubles,
        )
        if not valid:
            logger.error(
                "Rejected inconsistent successful payment for user %s",
                message.chat.id,
            )
            await message.answer(
                "Платёж получен, но его параметры требуют ручной проверки. "
                "Обратитесь в поддержку: @RaketManager"
            )
            return

        # Для ЮKassa сохраняется именно её номер транзакции: по нему платёж
        # можно найти в личном кабинете провайдера.
        provider_payment_id = (
            payment.provider_payment_charge_id
            if provider in {"yookassa", "yookassa_test"}
            else payment.telegram_payment_charge_id
        )
        assert plan is not None
        access, activated = await db.activate_plan(
            chat_id=message.chat.id,
            plan_code=plan.code,
            search_limit=plan.search_limit,
            duration_days=plan.duration_days,
            lifetime=plan.lifetime,
            provider=provider,
            provider_payment_id=provider_payment_id,
            amount=payment.total_amount,
            currency=payment.currency,
            invoice_payload=payment.invoice_payload,
        )
        monitor.wake()
        payment_accepted_text = (
            "✅ Тестовая оплата принята, тариф активирован.\n\n"
            if provider == "yookassa_test"
            else "✅ Оплата принята.\n\n"
        )
        await message.answer(
            (payment_accepted_text if activated else "ℹ️ Платёж уже учтён.\n\n")
            + _access_text(access, plans),
            parse_mode=ParseMode.HTML,
        )

    # ------------------------------------------------------------------
    # Просмотр состояния и управление сохранёнными поисками
    # ------------------------------------------------------------------

    @router.message(Command("list"))
    async def list_handler(message: Message) -> None:
        """Показать каждый поиск вместе с кнопками управления."""

        access = await db.get_access(message.chat.id)
        subscriptions = await db.list_subscriptions(message.chat.id)
        if not subscriptions:
            await message.answer(
                "У вас пока нет поисков. Отправьте ссылку поиска Avito.\n\n"
                + _access_text(access, plans),
                parse_mode=ParseMode.HTML,
            )
            return
        await message.answer(
            f"Ваши поиски: {len(subscriptions)}\n" + _access_text(access, plans),
            parse_mode=ParseMode.HTML,
        )
        for index, subscription in enumerate(subscriptions, start=1):
            state_text = "работает" if subscription.active else "приостановлена"
            if index > access.search_limit:
                state_text = "не входит в текущий тариф"
            init_text = (
                "исходная выдача сохранена"
                if subscription.initialized_at
                else "ожидает первой проверки"
            )
            safe_name = html.escape(_search_name(subscription.url))
            safe_url = html.escape(subscription.url, quote=True)
            await message.answer(
                f"<b>#{subscription.id} — {safe_name}</b>\n"
                f"Состояние: {state_text}\n"
                f"Интервал: {subscription.interval_minutes} мин\n"
                f"Инициализация: {init_text}\n"
                f'🔗 <a href="{safe_url}">Открыть поиск</a>',
                parse_mode=ParseMode.HTML,
                reply_markup=subscription_keyboard(subscription),
                disable_web_page_preview=True,
            )

    @router.message(Command("status"))
    async def status_handler(message: Message) -> None:
        """Показать время прошлой/следующей проверки и последнюю ошибку."""

        access = await db.get_access(message.chat.id)
        subscriptions = await db.list_subscriptions(message.chat.id)
        if not subscriptions:
            await message.answer(_access_text(access, plans), parse_mode=ParseMode.HTML)
            return
        lines = [_access_text(access, plans), "\n<b>Состояние поисков</b>"]
        for subscription in subscriptions:
            marker = "🟢" if subscription.active else "⏸"
            error = (
                f"\n  Ошибка: {html.escape(subscription.last_error[:250])}"
                if subscription.last_error
                else ""
            )
            lines.append(
                f"\n{marker} #{subscription.id} "
                f"{html.escape(_search_name(subscription.url))}\n"
                f"  Последняя: {format_dt(subscription.last_checked_at)}\n"
                f"  Следующая: {format_dt(subscription.next_check_at)}"
                f"{error}"
            )
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

    # ------------------------------------------------------------------
    # Диалог добавления нового URL: ссылка -> выбор интервала -> запись в БД
    # ------------------------------------------------------------------

    @router.message(F.text.startswith(("http://", "https://")))
    async def url_handler(message: Message, state: FSMContext) -> None:
        """Проверить URL и сохранить его во временном состоянии FSM."""

        access = await db.get_access(message.chat.id)
        if not access.active:
            await message.answer(
                _access_text(access, plans),
                parse_mode=ParseMode.HTML,
                reply_markup=plans_keyboard(plans),
            )
            return
        try:
            normalized = normalize_avito_url(message.text or "")
        except ValueError as error:
            await message.answer(f"Не удалось принять ссылку: {error}")
            return
        await state.set_state(AddSubscription.choosing_interval)
        await state.update_data(pending_url=normalized)
        await message.answer(
            f"<b>{html.escape(_search_name(normalized))}</b>\n"
            "Как часто проверять этот поиск?",
            parse_mode=ParseMode.HTML,
            reply_markup=interval_keyboard(
                minimum_minutes=access.min_interval_minutes
            ),
        )

    @router.callback_query(
        AddSubscription.choosing_interval, F.data.startswith("newint:")
    )
    async def new_interval_handler(callback: CallbackQuery, state: FSMContext) -> None:
        """Завершить создание поиска после выбора допустимого интервала."""

        if callback.message is None:
            return
        try:
            interval = int((callback.data or "").split(":")[1])
        except (IndexError, ValueError):
            await callback.answer("Некорректный интервал", show_alert=True)
            return
        if interval not in ALLOWED_INTERVALS:
            await callback.answer("Этот интервал не поддерживается", show_alert=True)
            return
        data = await state.get_data()
        url = data.get("pending_url")
        if not url:
            await state.clear()
            await callback.answer(
                "Данные устарели. Отправьте ссылку ещё раз.", show_alert=True
            )
            return
        try:
            access = await db.get_access(callback.message.chat.id)
            if not access.active:
                await state.clear()
                await callback.answer(
                    "Пробный период или подписка закончились",
                    show_alert=True,
                )
                await callback.message.answer(
                    _access_text(access, plans),
                    parse_mode=ParseMode.HTML,
                    reply_markup=plans_keyboard(plans),
                )
                return
            if interval < access.min_interval_minutes:
                await callback.answer(
                    f"Для вашего тарифа минимальный интервал — "
                    f"{access.min_interval_minutes} минут",
                    show_alert=True,
                )
                return
            subscription, created = await db.create_subscription(
                callback.message.chat.id,
                url,
                interval,
                min(access.search_limit, settings.max_subscriptions_per_user),
            )
        except ValueError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await state.clear()
        monitor.wake()
        verb = "добавлена" if created else "обновлена"
        await callback.message.edit_text(
            f"✅ Подписка #{subscription.id} {verb}.\n"
            f"Интервал: {interval} мин.\n\n"
            "Первая выдача будет сохранена без уведомлений."
        )
        logger.info(
            "Subscription %s %s for user %s",
            subscription.id,
            "created" if created else "updated",
            callback.message.chat.id,
        )
        await callback.answer()

    # ------------------------------------------------------------------
    # Inline-кнопки существующего поиска
    # ------------------------------------------------------------------

    @router.callback_query(F.data.startswith("sub:"))
    async def subscription_action_handler(callback: CallbackQuery) -> None:
        """Удалить, остановить, возобновить или немедленно проверить поиск."""

        if callback.message is None:
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await callback.answer("Некорректная команда", show_alert=True)
            return
        action = parts[1]
        try:
            subscription_id = int(parts[2])
        except ValueError:
            await callback.answer("Некорректная подписка", show_alert=True)
            return
        chat_id = callback.message.chat.id
        subscription = await db.get_subscription(subscription_id, chat_id)
        if subscription is None:
            await callback.answer("Подписка не найдена", show_alert=True)
            return

        if action == "delete":
            await db.delete_subscription(subscription_id, chat_id)
            await callback.message.edit_text(f"🗑 Подписка #{subscription_id} удалена.")
        elif action == "pause":
            await db.set_active(subscription_id, chat_id, False)
            await callback.message.edit_text(
                f"⏸ Подписка #{subscription_id} приостановлена."
            )
        elif action == "resume":
            await db.set_active(subscription_id, chat_id, True)
            monitor.wake()
            await callback.message.edit_text(
                f"▶️ Подписка #{subscription_id} возобновлена."
            )
        elif action == "check":
            await db.force_due(subscription_id, chat_id)
            monitor.wake()
            await callback.answer("Проверка поставлена в очередь")
            return
        elif action == "interval":
            access = await db.get_access(chat_id)
            await callback.message.answer(
                f"Новый интервал для подписки #{subscription_id}:",
                reply_markup=subscription_interval_keyboard(
                    subscription_id,
                    minimum_minutes=access.min_interval_minutes,
                ),
            )
        else:
            await callback.answer("Неизвестная команда", show_alert=True)
            return
        await callback.answer()

    @router.callback_query(F.data.startswith("subint:"))
    async def set_subscription_interval_handler(callback: CallbackQuery) -> None:
        """Сохранить новый период проверки существующего поиска."""

        if callback.message is None:
            return
        parts = (callback.data or "").split(":")
        try:
            subscription_id = int(parts[1])
            interval = int(parts[2])
        except (IndexError, ValueError):
            await callback.answer("Некорректная команда", show_alert=True)
            return
        if interval not in ALLOWED_INTERVALS:
            await callback.answer("Этот интервал не поддерживается", show_alert=True)
            return
        access = await db.get_access(callback.message.chat.id)
        if interval < access.min_interval_minutes:
            await callback.answer(
                f"Для вашего тарифа минимальный интервал — "
                f"{access.min_interval_minutes} минут",
                show_alert=True,
            )
            return
        changed = await db.set_interval(
            subscription_id, callback.message.chat.id, interval
        )
        if not changed:
            await callback.answer("Подписка не найдена", show_alert=True)
            return
        monitor.wake()
        await callback.message.edit_text(
            f"✅ Интервал подписки #{subscription_id}: {interval} мин."
        )
        await callback.answer()

    @router.message()
    async def fallback_handler(message: Message) -> None:
        """Ответить на текст, который не подошёл ни одному обработчику выше."""

        await message.answer(
            "Пришлите полную ссылку поиска Avito, начинающуюся с https://"
        )

    return router
