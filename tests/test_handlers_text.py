"""Проверки статических справочных сообщений бота."""

from avito_bot.handlers import FAQ_TEXT, HELP_TEXT


def test_faq_explains_simultaneous_searches() -> None:
    """FAQ должен ясно объяснять лимит одновременных ссылок Avito."""

    assert "одновременно следить за пятью разными ссылками Avito" in FAQ_TEXT
    assert "Каждый поиск работает независимо" in FAQ_TEXT
    assert "/faq — частые вопросы" in HELP_TEXT
    assert len(FAQ_TEXT) < 4096
