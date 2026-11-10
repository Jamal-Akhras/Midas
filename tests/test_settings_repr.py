"""Credentials must never appear in the Settings repr: pytest and logging
render the object on failures, and that output travels."""

from midas.config import Settings

SECRET_FIELDS = [
    "openai_api_key",
    "anthropic_api_key",
    "gemini_api_key",
    "alpaca_api_key",
    "alpaca_api_secret",
    "finnhub_api_key",
    "fmp_api_key",
    "brave_api_key",
    "tavily_api_key",
    "telegram_bot_token",
]


def test_repr_and_str_omit_every_credential_field():
    values = {name: f"DUMMY-{name.upper()}" for name in SECRET_FIELDS}
    s = Settings(_env_file=None, **values)
    rendered = repr(s) + str(s)
    for name, value in values.items():
        assert value not in rendered, name
        assert name not in rendered, name
        assert getattr(s, name) == value  # the code still reads the value
