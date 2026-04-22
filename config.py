import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_bot_token: str | None
    admin_id: int
    required_channels: list[str]
    subscription_file_path: str
    xui_url: str
    xui_username: str
    xui_password: str
    payment_card_number: str
    price_per_gb: int
    admin_cards: dict[int, str]


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _require_any(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    raise ValueError(f"Missing required environment variable. Expected one of: {', '.join(names)}")


def _parse_admin_cards(default_admin_id: int, default_card: str) -> dict[int, str]:
    raw = os.getenv("ADMINS", "").strip()
    admin_cards: dict[int, str] = {}

    if raw:
        # Format: "111111111:6037-xxxx,222222222:5892-xxxx"
        for item in raw.split(","):
            part = item.strip()
            if not part:
                continue
            if ":" not in part:
                raise ValueError("Invalid ADMINS format. Expected: id:card,id:card")
            admin_id_text, card_number = part.split(":", 1)
            admin_cards[int(admin_id_text.strip())] = card_number.strip()

    if default_admin_id not in admin_cards:
        admin_cards[default_admin_id] = default_card

    return admin_cards


def _parse_required_channels() -> list[str]:
    raw = os.getenv("REQUIRED_CHANNELS", "").strip()
    if raw:
        # Accept separators: English comma, Persian comma, semicolon, and new lines.
        parts = re.split(r"[,،;\n\r]+", raw)
        channels = [part.strip() for part in parts if part.strip()]
    else:
        channels = [os.getenv("REQUIRED_CHANNEL", "@PouriaDRD").strip()]

    normalized: list[str] = []
    for channel in channels:
        value = channel.strip()
        value = value.replace("https://t.me/", "").replace("http://t.me/", "")
        value = value.replace("t.me/", "")
        value = value.lstrip("/")
        if value.startswith("@"):
            normalized.append(value)
        else:
            normalized.append(f"@{value}")

    # Keep order but remove duplicates.
    deduped: list[str] = []
    seen: set[str] = set()
    for channel in normalized:
        if channel not in seen:
            deduped.append(channel)
            seen.add(channel)
    return deduped


def load_settings() -> Settings:
    default_admin_id = int(_require_any("ADMIN_ID", "ADMIN_TELEGRAM_ID"))
    default_card = os.getenv("PAYMENT_CARD_NUMBER", "0000-0000-0000-0000")
    return Settings(
        bot_token=_require_any("BOT_TOKEN", "TOKEN"),
        admin_bot_token=os.getenv("ADMIN_BOT_TOKEN"),
        admin_id=default_admin_id,
        required_channels=_parse_required_channels(),
        subscription_file_path=os.getenv("SUBSCRIPTION_FILE_PATH", "All-Inbounds.txt"),
        xui_url=_require_env("XUI_URL").rstrip("/"),
        xui_username=_require_env("XUI_USERNAME"),
        xui_password=_require_env("XUI_PASSWORD"),
        payment_card_number=default_card,
        price_per_gb=int(os.getenv("PRICE_PER_GB", "400")),
        admin_cards=_parse_admin_cards(default_admin_id, default_card),
    )
