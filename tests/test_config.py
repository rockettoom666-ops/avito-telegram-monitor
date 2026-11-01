from avito_bot.config import Settings


def test_avito_does_not_inherit_telegram_vpn(monkeypatch) -> None:
    """Прокси Telegram не должен незаметно становиться прокси Avito."""

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("PROXY_URL", "http://127.0.0.1:7890")
    monkeypatch.setenv("AVITO_PROXY_URL", "")

    settings = Settings.load()

    assert settings.proxy_url == "http://127.0.0.1:7890"
    assert settings.avito_proxy_url == ""
