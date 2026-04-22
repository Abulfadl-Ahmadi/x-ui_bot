from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import db


NORMAL_PRICE_DEFAULT = int(os.getenv("NORMAL_PRICE_DEFAULT", "600"))
SPECIAL_PRICE_DEFAULT = int(os.getenv("SPECIAL_PRICE_DEFAULT", "800"))


@dataclass(frozen=True)
class SubscriptionEntry:
    subscription_type: str
    price: int
    link: str


_URL_PATTERN = re.compile(r"(vless://\S+|https?://\S+)", re.IGNORECASE)


def normalize_subscription_type(value: str | None) -> str:
    text = (value or "").strip().lower()
    if not text:
        return "normal"

    if text in {"normal", "معمولی", "معمولي", "basic", "base", "regular"}:
        return "normal"
    if text in {"special", "ویژه", "ويژه", "vip", "prime", "speciale"}:
        return "special"

    if any(token in text for token in ("prime", "vip", "special", "ویژه", "ويژه")):
        return "special"
    return "normal"


def subscription_type_label(subscription_type: str) -> str:
    normalized = normalize_subscription_type(subscription_type)
    return "ویژه" if normalized == "special" else "معمولی"


def default_price_for_type(subscription_type: str) -> int:
    return SPECIAL_PRICE_DEFAULT if normalize_subscription_type(subscription_type) == "special" else NORMAL_PRICE_DEFAULT


def infer_subscription_type_from_link(link: str) -> str:
    fragment = ""
    if "#" in link:
        fragment = link.rsplit("#", 1)[1].lower()
    combined = f"{link.lower()} {fragment}"
    if any(token in combined for token in ("prime", "vip", "special", "ویژه", "ويژه")):
        return "special"
    return "normal"


def _extract_link_from_text(line: str) -> str | None:
    match = _URL_PATTERN.search(line)
    if not match:
        return None
    return match.group(1).strip().rstrip(",;|")


def _parse_price_token(token: str) -> int | None:
    cleaned = token.strip().replace("toman", "").replace("تومان", "")
    cleaned = re.sub(r"[^0-9]", "", cleaned)
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_record_from_text(line: str) -> SubscriptionEntry | None:
    link = _extract_link_from_text(line)
    if link is None:
        return None

    prefix = line[: line.index(link)].strip(" |,\t") if link in line else ""
    tokens = [token for token in re.split(r"[|,\t\s]+", prefix) if token]

    subscription_type: str | None = None
    price: int | None = None

    for token in tokens:
        normalized_type = normalize_subscription_type(token)
        if token.strip().lower() in {"normal", "special"} or token.strip() in {"معمولی", "ویژه"}:
            subscription_type = normalized_type
            continue
        if price is None:
            price = _parse_price_token(token)

    if subscription_type is None:
        subscription_type = infer_subscription_type_from_link(link)

    if price is None:
        price = default_price_for_type(subscription_type)

    return SubscriptionEntry(subscription_type=subscription_type, price=price, link=link)


def _coerce_record(item: Any) -> SubscriptionEntry | None:
    if isinstance(item, str):
        return _parse_record_from_text(item)

    if isinstance(item, dict):
        link = str(item.get("link") or item.get("url") or item.get("subscription") or "").strip()
        if not link:
            return None
        subscription_type = normalize_subscription_type(
            item.get("type") or item.get("subscription_type") or item.get("category") or item.get("name")
        )
        price_value = item.get("price")
        if isinstance(price_value, str):
            price = _parse_price_token(price_value)
        elif isinstance(price_value, int):
            price = price_value
        else:
            price = None

        if price is None:
            price = default_price_for_type(subscription_type)
        return SubscriptionEntry(subscription_type=subscription_type, price=price, link=link)

    return None


def load_subscription_entries(file_path: str | Path) -> list[SubscriptionEntry]:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(path)

    raw_text = path.read_text(encoding="utf-8-sig").strip()
    if not raw_text:
        return []

    if path.suffix.lower() == ".json" or raw_text.startswith("[") or raw_text.startswith("{"):
        payload = json.loads(raw_text)
        items: list[Any]
        if isinstance(payload, dict):
            items = payload.get("subscriptions") or payload.get("items") or payload.get("links") or []
        elif isinstance(payload, list):
            items = payload
        else:
            items = []

        entries: list[SubscriptionEntry] = []
        for item in items:
            record = _coerce_record(item)
            if record is not None:
                entries.append(record)
        return entries

    entries: list[SubscriptionEntry] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        record = _parse_record_from_text(line)
        if record is not None:
            entries.append(record)
    return entries


def resolve_subscription_file_path(configured_path: str, base_dir: Path | None = None) -> Path:
    base_path = base_dir or Path(__file__).resolve().parent
    configured = Path(configured_path)

    candidates: list[Path] = []
    if configured.is_absolute():
        candidates.append(configured)
    else:
        candidates.extend([base_path / configured, configured])

    candidates.extend(
        [
            base_path / "All-Inbounds-Subs.txt",
            base_path / "All-Inbounds.txt",
            base_path / "All-Inbounds-Subs.json",
            base_path / "All-Inbounds.json",
        ]
    )

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            return candidate

    return candidates[0]


def sync_subscription_inventory_from_file(admin_id: int, configured_path: str, base_dir: Path | None = None) -> tuple[Path, int]:
    file_path = resolve_subscription_file_path(configured_path, base_dir)
    base_path = base_dir or Path(__file__).resolve().parent

    file_name = file_path.name.lower()
    if "subs" in file_name:
        subs_file = file_path
        config_candidates = [
            file_path.with_name("All-Inbounds.txt"),
            base_path / "All-Inbounds.txt",
        ]
        config_file = next((candidate for candidate in config_candidates if candidate.exists()), file_path.with_name("All-Inbounds.txt"))
    else:
        config_file = file_path
        subs_candidates = [
            file_path.with_name("All-Inbounds-Subs.txt"),
            base_path / "All-Inbounds-Subs.txt",
        ]
        subs_file = next((candidate for candidate in subs_candidates if candidate.exists()), None)

    records: list[dict[str, object]] = []
    if subs_file and config_file.exists():
        config_lines = [line.strip() for line in config_file.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        sub_lines = [line.strip() for line in subs_file.read_text(encoding="utf-8-sig").splitlines() if line.strip()]

        pair_count = min(len(config_lines), len(sub_lines))
        for index in range(pair_count):
            config_link = _extract_link_from_text(config_lines[index])
            sub_link = _extract_link_from_text(sub_lines[index])
            if not config_link or not sub_link:
                continue
            subscription_type = infer_subscription_type_from_link(config_link)
            price = default_price_for_type(subscription_type)
            records.append(
                {
                    "subscription_type": subscription_type,
                    "price": price,
                    "config_link": config_link,
                    "sub_link": sub_link,
                    "link": config_link,
                }
            )
    else:
        entries = load_subscription_entries(file_path)
        records = [
            {
                "subscription_type": entry.subscription_type,
                "price": entry.price,
                "config_link": entry.link,
                "sub_link": None,
                "link": entry.link,
            }
            for entry in entries
        ]

    inserted = db.sync_subscription_inventory(0, records)
    return file_path, inserted
