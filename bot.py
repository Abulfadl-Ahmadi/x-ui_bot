import asyncio
import html
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from config import load_settings
from subscriptions import default_price_for_type, resolve_subscription_file_path, subscription_type_label, sync_subscription_inventory_from_file
from xui import XUIClient, XUIError


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


settings = load_settings()
xui_client = XUIClient(
    base_url=settings.xui_url,
    username=settings.xui_username,
    password=settings.xui_password,
)
RECEIPTS_DIR = Path(__file__).resolve().parent / "receipts"


def _join_channel_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, channel in enumerate(settings.required_channels, start=1):
        channel_url = channel.lstrip("@")
        rows.append([InlineKeyboardButton(f"عضویت در چنل {index}", url=f"https://t.me/{channel_url}")])
    rows.append([InlineKeyboardButton("تایید عضویت ✅", callback_data="check_membership")])
    return InlineKeyboardMarkup(rows)


def _channels_text() -> str:
    return "، ".join(settings.required_channels)


def _is_chat_member_joined(member: object) -> bool:
    status = getattr(member, "status", None)
    is_member = getattr(member, "is_member", None)
    return status in {"member", "administrator", "creator"} or bool(is_member)


async def _ensure_channel_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user is None:
        return False

    is_confirm_callback = bool(update.callback_query and update.callback_query.data == "check_membership")

    try:
        joined_all = True
        for channel in settings.required_channels:
            member = await context.bot.get_chat_member(channel, user.id)
            if not _is_chat_member_joined(member):
                joined_all = False
                break

        if joined_all:
            if is_confirm_callback:
                context.user_data["channel_verified"] = True
                return True

            if context.user_data.get("channel_verified"):
                return True

            if update.callback_query:
                await update.callback_query.answer(
                    "عضویت شما شناسایی شد، لطفا روی تایید عضویت بزنید.",
                    show_alert=True,
                )
            elif update.message:
                await update.message.reply_text(
                    "عضویت شما شناسایی شد، حالا روی تایید عضویت بزنید تا وارد خدمات شوید.",
                    reply_markup=_join_channel_keyboard(),
                )
            return False
    except (BadRequest, Forbidden):
        logger.exception("Failed to check channel membership due to bot/channel permissions")
        message = (
            "تایید عضویت موقتا در دسترس نیست.\n"
            "⚠️ ادمین باید ربات را در چنل ادمین کند و آیدی چنل را درست بگذارد."
        )
        if update.message:
            await update.message.reply_text(message, reply_markup=_join_channel_keyboard())
        elif update.callback_query:
            await update.callback_query.answer(message, show_alert=True)
        return False
    except Exception:  # noqa: BLE001
        logger.exception("Failed to check channel membership")

    if is_confirm_callback:
        if update.callback_query:
            await update.callback_query.answer(
                f"ابتدا در همه چنل‌ها عضو شوید: {_channels_text()}", show_alert=True
            )
        return False

    if update.message:
        await update.message.reply_text(
            f"برای استفاده از ربات باید ابتدا در همه چنل‌ها عضو شوید:\n{_channels_text()}",
            reply_markup=_join_channel_keyboard(),
        )
    elif update.callback_query:
        await update.callback_query.answer(
            f"ابتدا در همه چنل‌ها عضو شوید: {_channels_text()}", show_alert=True
        )
    return False


def _clear_states(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["waiting_for_referral"] = False
    context.user_data["waiting_for_order_referral"] = False
    context.user_data["waiting_for_volume"] = False
    context.user_data["waiting_for_subscription_type"] = False
    context.user_data["waiting_for_discount"] = False
    context.user_data["waiting_for_receipt"] = False
    context.user_data.pop("pending_referral", None)
    context.user_data.pop("pending_admin_id", None)
    context.user_data.pop("pending_volume", None)
    context.user_data.pop("pending_subscription_type", None)
    context.user_data.pop("pending_base_price", None)
    context.user_data.pop("pending_subscription_source_mode", None)


def _subscription_source_mode_label(mode: str) -> str:
    return "فایل" if str(mode).strip().lower() == "file" else "XUI"


def _subscription_price_for_type(admin_id: int, subscription_type: str, source_mode: str) -> int:
    normalized_type = subscription_type_label(subscription_type)
    if str(source_mode).strip().lower() == "file":
        row = db.peek_available_subscription(admin_id, "special" if normalized_type == "ویژه" else "normal")
        if row is not None and int(row["price"] or 0) > 0:
            return int(row["price"])
    return default_price_for_type(subscription_type)


def _subscription_type_keyboard(admin_id: int, source_mode: str) -> InlineKeyboardMarkup | None:
    mode = str(source_mode).strip().lower()
    normal_price = _subscription_price_for_type(admin_id, "normal", mode)
    special_price = _subscription_price_for_type(admin_id, "special", mode)

    rows: list[list[InlineKeyboardButton]] = []
    if mode == "file":
        normal_available = db.peek_available_subscription(admin_id, "normal") is not None
        special_available = db.peek_available_subscription(admin_id, "special") is not None
        if normal_available:
            rows.append([InlineKeyboardButton(f"معمولی - {_format_toman(normal_price)}", callback_data="subscription_type:normal")])
        if special_available:
            rows.append([InlineKeyboardButton(f"ویژه - {_format_toman(special_price)}", callback_data="subscription_type:special")])
        if not rows:
            return None
    else:
        rows = [
            [InlineKeyboardButton(f"معمولی - {_format_toman(normal_price)}", callback_data="subscription_type:normal")],
            [InlineKeyboardButton(f"ویژه - {_format_toman(special_price)}", callback_data="subscription_type:special")],
        ]

    return InlineKeyboardMarkup(rows)


def _sync_subscription_inventory_for_admin(admin_id: int) -> tuple[Path | None, int]:
    mode = db.get_admin_subscription_source_mode(admin_id)
    if mode != "file":
        return None, 0

    file_path = resolve_subscription_file_path(settings.subscription_file_path, Path(__file__).resolve().parent)
    if not file_path.exists():
        return file_path, 0

    synced_file, inserted = sync_subscription_inventory_from_file(
        admin_id,
        settings.subscription_file_path,
        Path(__file__).resolve().parent,
    )
    return synced_file, inserted


def _terms_text() -> str:
    return """🔴 <b>شرایط فروش</b> ⚠️
<i>لطفاً با دقت مطالعه گردد:</i>

۱. به دلیل ناپایداری شبکه اینترنت در کشور، احتمال قطع شدن پیکربندی‌ها در هر لحظه وجود دارد. 🌐❌

۲. در صورت بروز قطعی، تمام تلاش برای برقراری مجدد ارتباط به عمل خواهد آمد، اما این امر تضمین‌شده نمی‌باشد. 🔧⚠️

۳. با توجه به هزینه‌های بالای سرورها و محدودیت‌های اعمال‌شده از سوی ارائه‌دهندگان در خصوص بازگشت سرورهای خریداری‌شده، در صورت قطعی کامل، امکان استرداد وجه وجود نخواهد داشت. 💰🚫

۴. اگرچه پیکربندی‌ها در حال حاضر فعال هستند، احتمال قطع ارتباط در ساعات آینده و بروز قطعی کامل محتمل است. ⏳📉

<b>لطفاً تنها در صورتی که با کلیه شرایط فوق موافقت داشته باشید، نسبت به خرید اقدام نمایید.</b> ✅📝"""


def _terms_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ موافقم با شرایط", callback_data="accept_terms")],
        ]
    )


def _main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("خرید"), KeyboardButton("اشتراک ها")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _get_latest_active_order(user_id: int):
    return db.get_latest_approved_order(user_id)


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_cards


def _get_admin_card(admin_id: int) -> str:
    return settings.admin_cards.get(admin_id, settings.payment_card_number)


def _resolve_user_admin_id(user_row) -> int:
    if user_row is None:
        return settings.admin_id

    admin_id = user_row["admin_id"]
    if admin_id is not None:
        return int(admin_id)

    referral = user_row["referral"]
    owner = db.get_referral_code_owner(referral) if referral else None
    if owner is not None:
        return int(owner)
    return settings.admin_id


def _format_toman(amount: int) -> str:
    return f"{amount:,} تومان"


def _calculate_total_price(volume_gb: int) -> int:
    return volume_gb * settings.price_per_gb


def _apply_discount(base_price: int, percent: int) -> int:
    discounted = base_price - ((base_price * percent) // 100)
    return max(discounted, 0)


def _sanitize_for_email(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]", "", value)
    return clean[:24] or "ref"


def _build_client_email(referral_code: str, order_id: str) -> str:
    return f"{_sanitize_for_email(referral_code)}-{order_id.replace('-', '')[:6]}"


def _normalize_vless_link(raw_link: str) -> str:
    parsed = urlsplit(raw_link)
    if parsed.scheme != "vless":
        return raw_link

    if "@" not in parsed.netloc:
        return raw_link

    user_info, _, host_port = parsed.netloc.rpartition("@")
    host, sep, port = host_port.partition(":")
    if not host:
        return raw_link

    target_host = os.getenv("PUBLIC_SUB_HOST") or os.getenv("XUI_PUBLIC_HOST") or host
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    forced_type = os.getenv("PUBLIC_SUB_TYPE", "httpupgrade")
    forced_path = os.getenv("PUBLIC_SUB_PATH", "/assets")

    normalized_params = {
        "encryption": params.get("encryption", "none"),
        "security": "tls",
        "sni": target_host,
        "fp": params.get("fp", "chrome"),
        "type": forced_type,
        "host": target_host,
        "path": forced_path,
    }

    netloc = f"{user_info}@{target_host}"
    if sep:
        netloc = f"{netloc}:{port}"

    # Keep '%' untouched (for already-encoded values) but encode '/' to get path like %2Fassets.
    query = urlencode(normalized_params, safe="%")
    return urlunsplit(("vless", netloc, parsed.path, query, parsed.fragment))


def _bytes_to_gb_text(value: int) -> str:
    if value <= 0:
        return "نامشخص"
    gb = value / (1024 ** 3)
    return f"{gb:.2f} گیگابایت"


def _remaining_time_text(expiry_ms: int) -> str:
    if expiry_ms <= 0:
        return "نامشخص"

    expiry_dt = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    if expiry_dt <= now:
        return "منقضی شده"

    delta = expiry_dt - now
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60

    if days > 0:
        return f"{days} روز و {hours} ساعت"
    if hours > 0:
        return f"{hours} ساعت و {minutes} دقیقه"
    return f"{minutes} دقیقه"


def _fallback_expiry_from_order(created_at: str) -> int:
    try:
        created_dt = datetime.fromisoformat(created_at)
    except ValueError:
        return 0

    if created_dt.tzinfo is None:
        created_dt = created_dt.replace(tzinfo=timezone.utc)

    days_valid = int(os.getenv("XUI_DAYS_VALID", "30"))
    expiry_dt = created_dt + timedelta(days=days_valid)
    return int(expiry_dt.timestamp() * 1000)


async def _format_subscriptions_fa(user_id: int) -> str:
    orders = db.get_all_approved_orders(user_id)
    if not orders:
        return "🗂️ هنوز اشتراک فعالی برای شما ثبت نشده است."

    cards: list[str] = ["📦 <b>لیست اشتراک‌های شما</b>"]
    for index, order in enumerate(orders, start=1):
        source_mode = str(order["subscription_source_mode"] or "xui").strip().lower()
        raw_config_link = str(order["vpn_link"] or "").strip()
        display_config_link = raw_config_link if source_mode == "file" else _normalize_vless_link(raw_config_link)
        display_sub_link = str(order["sub_link"] or "").strip()
        subscription_type = subscription_type_label(str(order["subscription_type"] or "normal"))

        status = None
        try:
            status = await asyncio.to_thread(xui_client.get_client_status, order["vpn_link"])
        except Exception:  # noqa: BLE001
            logger.exception("خطا در دریافت وضعیت اشتراک از X-UI")

        total_text = f"{order['volume']} گیگابایت"
        used_text = "نامشخص"
        remaining_text = total_text
        expiry_ms = _fallback_expiry_from_order(order["created_at"])

        if status:
            if status.get("total_bytes", 0) > 0:
                total_text = _bytes_to_gb_text(status["total_bytes"])
            used_text = _bytes_to_gb_text(status.get("used_bytes", 0))
            if status.get("remaining_bytes", 0) > 0:
                remaining_text = _bytes_to_gb_text(status["remaining_bytes"])
            else:
                remaining_text = "0.00 گیگابایت"
            if status.get("expiry_ms", 0) > 0:
                expiry_ms = status["expiry_ms"]

        remaining_time = _remaining_time_text(expiry_ms)
        cards.append(
            "\n".join(
                [
                    f"\n<b>🔹 اشتراک {index}</b>",
                    f"🆔 شناسه سفارش: <code>{html.escape(order['id'])}</code>",
                    f"🧩 نوع اشتراک: <b>{subscription_type}</b>",
                    f"📊 حجم کل: <b>{total_text}</b>",
                    f"📉 حجم مصرف‌شده: <b>{used_text}</b>",
                    f"📦 حجم باقی‌مانده: <b>{remaining_text}</b>",
                    f"⏳ زمان باقی‌مانده: <b>{remaining_time}</b>",
                    "🔗 لینک کانفیگ:",
                    f"<pre><code>{html.escape(display_config_link)}</code></pre>",
                    "🌐 لینک ساب:",
                    f"<pre><code>{html.escape(display_sub_link or '-')}</code></pre>",
                ]
            )
        )

    return "\n\n".join(cards)


async def _build_subscription_message(order: dict, index: int) -> str:
    source_mode = str(order["subscription_source_mode"] or "xui").strip().lower()
    raw_config_link = str(order["vpn_link"] or "").strip()
    display_config_link = raw_config_link if source_mode == "file" else _normalize_vless_link(raw_config_link)
    display_sub_link = str(order["sub_link"] or "").strip()
    subscription_type = subscription_type_label(str(order["subscription_type"] or "normal"))

    status = None
    try:
        status = await asyncio.to_thread(xui_client.get_client_status, order["vpn_link"])
    except Exception:  # noqa: BLE001
        logger.exception("خطا در دریافت وضعیت اشتراک از X-UI")

    total_text = f"{order['volume']} گیگابایت"
    used_text = "نامشخص"
    remaining_text = total_text
    expiry_ms = _fallback_expiry_from_order(order["created_at"])

    if status:
        if status.get("total_bytes", 0) > 0:
            total_text = _bytes_to_gb_text(status["total_bytes"])
        used_text = _bytes_to_gb_text(status.get("used_bytes", 0))
        if status.get("remaining_bytes", 0) > 0:
            remaining_text = _bytes_to_gb_text(status["remaining_bytes"])
        else:
            remaining_text = "0.00 گیگابایت"
        if status.get("expiry_ms", 0) > 0:
            expiry_ms = status["expiry_ms"]

    remaining_time = _remaining_time_text(expiry_ms)
    return "\n".join(
        [
            f"📦 <b>اشتراک {index}</b>",
            f"🆔 شناسه سفارش: <code>{html.escape(order['id'])}</code>",
            f"🧩 نوع اشتراک: <b>{subscription_type}</b>",
            f"📊 حجم کل: <b>{total_text}</b>",
            f"📉 حجم مصرف‌شده: <b>{used_text}</b>",
            f"📦 حجم باقی‌مانده: <b>{remaining_text}</b>",
            f"⏳ زمان باقی‌مانده: <b>{remaining_time}</b>",
            "🔗 لینک کانفیگ:",
            f"<pre><code>{html.escape(display_config_link)}</code></pre>",
            "🌐 لینک ساب:",
            f"<pre><code>{html.escape(display_sub_link or '-')}</code></pre>",
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    _clear_states(context)
    user_id = update.effective_user.id
    user = db.get_user(user_id)

    if user is None:
        context.user_data["waiting_for_referral"] = True
        await update.message.reply_text(
            "سلام 👋\nکد معرف خود را ارسال کنید.", reply_markup=_main_menu_keyboard()
        )
        return

    await update.message.reply_text(
        "خوش آمدید 🌟\nاز منوی زیر استفاده کنید.", reply_markup=_main_menu_keyboard()
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    _clear_states(context)
    user = db.get_user(update.effective_user.id)
    if user is None:
        context.user_data["waiting_for_referral"] = True
        await update.message.reply_text(
            "ابتدا کد معرف را ارسال کنید، سپس می‌توانید از منو استفاده کنید.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    await update.message.reply_text(
        "منوی اصلی:",
        reply_markup=_main_menu_keyboard(),
    )


async def terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    _clear_states(context)
    await update.message.reply_text(
        _terms_text(),
        parse_mode="HTML",
        reply_markup=_terms_keyboard(),
    )


async def add_refcode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.message.reply_text("شما مجاز به استفاده از این دستور نیستید.")
        return

    codes = [code.strip() for code in context.args if code.strip()]
    if not codes and update.message.text:
        parts = update.message.text.split(maxsplit=1)
        if len(parts) > 1:
            raw_codes = parts[1].replace(",", " ").split()
            codes = [code.strip() for code in raw_codes if code.strip()]

    if not codes:
        await update.message.reply_text("نمونه استفاده: /addrefcode CODE1 CODE2 CODE3")
        return

    inserted = db.add_referral_codes(codes, user_id)
    skipped = len(codes) - inserted
    await update.message.reply_text(
        f"کدهای معرف ثبت شدند.\n"
        f"ادمین مالک: {user_id}\n"
        f"تعداد افزوده‌شده: {inserted}\n"
        f"تکراری: {skipped}"
    )


async def confirm_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    query = update.callback_query
    if query:
        await query.answer("عضویت شما تایید شد ✅", show_alert=True)
        if query.message:
            await query.message.reply_text(
                "عضویت شما تایید شد ✅\nحالا از منوی زیر استفاده کنید.",
                reply_markup=_main_menu_keyboard(),
            )


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    user_id = update.effective_user.id
    user = db.get_user(user_id)
    if user is None:
        await update.message.reply_text("ابتدا /start را بزنید و کد معرف را ثبت کنید.")
        return

    if not db.user_accepted_terms(user_id):
        await update.message.reply_text(
            _terms_text(),
            parse_mode="HTML",
            reply_markup=_terms_keyboard(),
        )
        return

    if db.has_pending_order(user_id):
        pending = db.get_latest_pending_order(user_id)
        admin_id = int(pending["admin_id"] or _resolve_user_admin_id(user)) if pending else _resolve_user_admin_id(user)
        await update.message.reply_text(
            "شما یک سفارش در انتظار دارید. لطفا اول رسید همان سفارش را ارسال کنید.\n"
            f"💳 کارت پرداخت: {_get_admin_card(admin_id)}"
        )
        context.user_data["waiting_for_receipt"] = True
        return

    _clear_states(context)
    context.user_data["waiting_for_order_referral"] = True
    await update.message.reply_text(
        "برای این خرید، کد معرف را ارسال کنید."
    )


async def myaccount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    await show_all_subscriptions(update, context)


async def show_all_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    user_id = update.effective_user.id
    orders = db.get_all_approved_orders(user_id)
    if not orders:
        await update.message.reply_text(
            "🗂️ هنوز اشتراک فعالی برای شما ثبت نشده است.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    for index, order in enumerate(orders, start=1):
        message = await _build_subscription_message(order, index)
        await update.message.reply_text(
            message,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    await update.message.reply_text("پایان لیست اشتراک‌ها ✅", reply_markup=_main_menu_keyboard())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    if context.user_data.get("waiting_for_referral"):
        if not text:
            await update.message.reply_text("کد معرف نمی‌تواند خالی باشد. دوباره تلاش کنید.")
            return

        if not db.is_referral_code_valid(text):
            await update.message.reply_text("کد معرف معتبر نیست. لطفا کد درست وارد کنید.")
            return

        referral_admin_id = db.get_referral_code_owner(text)
        if referral_admin_id is None:
            referral_admin_id = settings.admin_id

        db.save_user(user_id, text, int(referral_admin_id))
        _clear_states(context)
        await update.message.reply_text(
            "کد معرف با موفقیت ثبت شد ✅\nاز منوی زیر استفاده کنید.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    if text == "خرید":
        await buy(update, context)
        return

    if text == "اشتراک ها":
        await show_all_subscriptions(update, context)
        return

    if context.user_data.get("waiting_for_order_referral"):
        if not db.is_referral_code_valid(text):
            await update.message.reply_text("کد معرف معتبر نیست. لطفا کد درست وارد کنید.")
            return

        admin_id = db.get_referral_code_owner(text)
        if admin_id is None:
            admin_id = settings.admin_id

        if not db.is_admin_sales_open(int(admin_id)):
            await update.message.reply_text(
                "فروش این معرف فعلا بسته است. لطفا بعدا دوباره تلاش کنید یا کد معرف دیگری بزنید."
            )
            return

        context.user_data["pending_referral"] = text
        context.user_data["pending_admin_id"] = int(admin_id)
        context.user_data["waiting_for_order_referral"] = False
        context.user_data["waiting_for_volume"] = True
        await update.message.reply_text(
            "حجم VPN را به گیگابایت وارد کنید (فقط عدد).\n"
            "فقط ۱ گیگ مجاز هست.\n"
            "اگر عدد دیگری وارد کنید، فقط ۱ گیگ فروش دارم."
        )
        return

    if context.user_data.get("waiting_for_volume"):
        if not text.isdigit():
            await update.message.reply_text("حجم باید عدد صحیح باشد. دوباره تلاش کنید.")
            return

        volume = int(text)
        if volume != 1:
            await update.message.reply_text("فقط ۱ گیگ فروش دارم.")
            return

        admin_id = int(context.user_data.get("pending_admin_id", settings.admin_id))
        source_mode = db.get_admin_subscription_source_mode(admin_id)
        type_keyboard = _subscription_type_keyboard(admin_id, source_mode)
        if type_keyboard is None:
            _clear_states(context)
            await update.message.reply_text(
                "فعلا هیچ اشتراک فعالی برای فروش موجود نیست.",
                reply_markup=_main_menu_keyboard(),
            )
            return

        if db.has_pending_order(user_id):
            _clear_states(context)
            context.user_data["waiting_for_receipt"] = True
            user_row = db.get_user(user_id)
            admin_id = _resolve_user_admin_id(user_row)
            await update.message.reply_text(
                "شما یک سفارش در انتظار دارید. لطفا عکس رسید همان سفارش را ارسال کنید.\n"
                f"💳 کارت پرداخت: {_get_admin_card(admin_id)}"
            )
            return

        context.user_data["pending_volume"] = volume
        context.user_data["waiting_for_volume"] = False
        context.user_data["waiting_for_subscription_type"] = True
        await update.message.reply_text(
            "نوع اشتراک را انتخاب کنید:"
            , reply_markup=type_keyboard
        )
        return

    if context.user_data.get("waiting_for_subscription_type"):
        await update.message.reply_text("لطفا از دکمه‌های نوع اشتراک استفاده کنید.")
        return

    if context.user_data.get("waiting_for_discount"):
        referral_code = context.user_data.get("pending_referral")
        admin_id = int(context.user_data.get("pending_admin_id", settings.admin_id))
        volume = int(context.user_data.get("pending_volume", 0))
        subscription_type = str(context.user_data.get("pending_subscription_type", "")).strip()
        source_mode = str(context.user_data.get("pending_subscription_source_mode", "xui")).strip().lower()
        base_price = int(context.user_data.get("pending_base_price", 0))

        if not referral_code or volume <= 0 or subscription_type not in {"normal", "special"}:
            _clear_states(context)
            await update.message.reply_text("فرآیند خرید ناقص شد. لطفا دوباره از دکمه خرید شروع کنید.")
            return

        discount_code: str | None = None
        discount_percent = 0
        if text not in {"ندارم", "ندارم.", "no", "NO"}:
            discount = db.validate_discount_code(text, admin_id)
            if discount is None:
                await update.message.reply_text(
                    "کد تخفیف نامعتبر است یا سقف استفاده آن تمام شده است.\n"
                    "دوباره کد صحیح بفرستید یا بنویسید: ندارم"
                )
                return
            discount_code = str(discount["code"])
            discount_percent = int(discount["percent"])

        if base_price <= 0:
            base_price = default_price_for_type(subscription_type)

        total_price = base_price
        final_price = _apply_discount(total_price, discount_percent)

        order_id = str(uuid.uuid4())
        db.create_order(
            order_id,
            user_id,
            volume,
            admin_id,
            referral_code,
            subscription_type,
            source_mode,
            base_price,
            discount_code,
            discount_percent,
            final_price,
        )
        if discount_code is not None:
            db.consume_discount_code(discount_code)

        db.save_user(user_id, referral_code, admin_id)
        context.user_data["current_order_id"] = order_id

        _clear_states(context)
        context.user_data["waiting_for_receipt"] = True
        payment_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("لغو ❌", callback_data=f"cancel:{order_id}")]]
        )
        await update.message.reply_text(
            "✅ سفارش شما ثبت شد.\n"
            f"🆔 شناسه سفارش: {order_id}\n"
            f"🏷️ کد معرف: {referral_code}\n"
            f"📦 حجم: {volume} گیگابایت\n"
            f"🧩 نوع اشتراک: {subscription_type_label(subscription_type)}\n\n"
            f"💵 مبلغ پایه: {_format_toman(total_price)}\n"
            f"🎁 تخفیف: {discount_percent}%\n"
            f"💰 مبلغ قابل پرداخت: {_format_toman(final_price)}\n"
            f"💳 کارت پرداخت: {_get_admin_card(admin_id)}\n"
            "پس از پرداخت، لطفا عکس رسید را ارسال کنید.",
            reply_markup=payment_keyboard,
        )
        return

    await update.message.reply_text("دستور نامعتبر است. از دکمه‌های منو استفاده کنید.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    user_id = update.effective_user.id

    if not context.user_data.get("waiting_for_receipt") and not db.has_pending_order(user_id):
        await update.message.reply_text("سفارش در انتظاری پیدا نشد. از دکمه خرید استفاده کنید.")
        return

    order = db.get_latest_pending_order(user_id)
    if order is None:
        context.user_data["waiting_for_receipt"] = False
        await update.message.reply_text("سفارش در انتظاری پیدا نشد. از دکمه خرید استفاده کنید.")
        return

    if not update.message.photo:
        await update.message.reply_text("لطفا یک عکس معتبر از رسید ارسال کنید.")
        return

    receipt_file_id = update.message.photo[-1].file_id
    receipt_local_path: str | None = None
    try:
        RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
        telegram_file = await context.bot.get_file(receipt_file_id)
        target_path = RECEIPTS_DIR / f"{order['id']}.jpg"
        await telegram_file.download_to_drive(custom_path=str(target_path))
        receipt_local_path = str(target_path)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to store receipt locally")

    db.save_receipt(order["id"], receipt_file_id, receipt_local_path)

    user = db.get_user(user_id)
    referral = user["referral"] if user else "-"
    username = update.effective_user.username
    telegram_identity = f"@{username}" if username else str(user_id)
    subscription_type = subscription_type_label(str(order["subscription_type"] or "normal"))

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Approve ✅", callback_data=f"approve:{order['id']}"),
            InlineKeyboardButton("Reject ❌", callback_data=f"reject:{order['id']}"),
        ]]
    )

    caption = (
        "🧾 رسید پرداخت جدید\n"
        f"🆔 سفارش: {order['id']}\n"
        f"👮 ادمین مسئول: {order['admin_id']}\n"
        f"👤 کاربر: {telegram_identity}\n"
        f"🏷️ کد معرف: {order['referral_code'] or '-'}\n"
        f"🧩 نوع اشتراک: {subscription_type}\n"
        f"📦 حجم: {order['volume']} گیگابایت\n"
        f"🎁 تخفیف: {int(order['discount_percent'] or 0)}%\n"
        f"💰 مبلغ پایه: {_format_toman(int(order['base_price'] or order['final_price'] or 0))}\n"
        f"💰 مبلغ نهایی: {_format_toman(int(order['final_price'] or 0))}\n"
        f"🏷️ معرف: {referral}"
    )

    admin_chat_id = int(order["admin_id"] or settings.admin_id)
    try:
        await context.bot.send_photo(
            chat_id=admin_chat_id,
            photo=receipt_file_id,
            caption=caption,
            reply_markup=keyboard,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to forward receipt by file_id, fallback to local file")
        if receipt_local_path and Path(receipt_local_path).exists():
            with Path(receipt_local_path).open("rb") as image_file:
                await context.bot.send_photo(
                    chat_id=admin_chat_id,
                    photo=image_file,
                    caption=caption,
                    reply_markup=keyboard,
                )
        else:
            await context.bot.send_message(
                chat_id=admin_chat_id,
                text=caption + "\n\n⚠️ عکس رسید در دسترس نبود.",
            )

    context.user_data["waiting_for_receipt"] = False
    await update.message.reply_text("رسید ارسال شد ✅\nدر انتظار تایید ادمین.")


async def accept_terms_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    query = update.callback_query
    user_id = query.from_user.id

    db.accept_terms(user_id)

    await query.answer("✅ شرایط پذیرفته شدند", show_alert=False)
    await query.edit_message_text(
        "✅ <b>شرایط پذیرفته شدند</b>\n\nاکنون می‌توانید از خدمات استفاده کنید.",
        parse_mode="HTML",
    )


async def subscription_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    query = update.callback_query

    data = query.data or ""
    if not data.startswith("subscription_type:"):
        await query.answer("عملیات نامعتبر", show_alert=True)
        return

    if not context.user_data.get("waiting_for_subscription_type"):
        await query.answer("این مرحله دیگر فعال نیست", show_alert=True)
        return

    subscription_type = data.split(":", 1)[1].strip().lower()
    if subscription_type not in {"normal", "special"}:
        await query.answer("نوع نامعتبر است", show_alert=True)
        return

    admin_id = int(context.user_data.get("pending_admin_id", settings.admin_id))
    if not db.is_admin_subscription_type_sales_open(admin_id, subscription_type):
        await query.answer("❌ تکمیل ظرفیت", show_alert=True)
        # صفحه را همان‌طور که هست نگه دار تا کاربر دوباره تلاش کند
        return

    source_mode = db.get_admin_subscription_source_mode(admin_id)

    if source_mode == "file":
        sync_subscription_inventory_from_file(
            admin_id,
            settings.subscription_file_path,
            Path(__file__).resolve().parent,
        )
        available = db.peek_available_subscription(admin_id, subscription_type)
        if available is None:
            await query.answer("فعلا این نوع اشتراک موجود نیست", show_alert=True)
            return
        base_price = int(available["price"] or default_price_for_type(subscription_type))
    else:
        base_price = default_price_for_type(subscription_type)

    context.user_data["pending_subscription_type"] = subscription_type
    context.user_data["pending_base_price"] = base_price
    context.user_data["pending_subscription_source_mode"] = source_mode
    context.user_data["waiting_for_subscription_type"] = False
    context.user_data["waiting_for_discount"] = True

    await query.answer()

    await query.edit_message_text(
        "✅ نوع اشتراک انتخاب شد.\n\n"
        f"نوع: <b>{subscription_type_label(subscription_type)}</b>\n"
        f"قیمت پایه: <b>{_format_toman(base_price)}</b>\n\n"
        "اگر کد تخفیف دارید ارسال کنید.\n"
        "اگر ندارید بنویسید: ندارم",
        parse_mode="HTML",
    )


async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    query = update.callback_query
    await query.answer()

    if not _is_admin(query.from_user.id):
        await query.answer("اجازه دسترسی ندارید", show_alert=True)
        return

    data = query.data or ""
    if not data.startswith("approve:"):
        await query.answer("عملیات نامعتبر", show_alert=True)
        return

    order_id = data.split(":", 1)[1]
    order = db.get_order(order_id)
    if order is None:
        await query.edit_message_caption(caption="سفارش پیدا نشد.")
        return

    order_admin_id = int(order["admin_id"] or settings.admin_id)
    if query.from_user.id != order_admin_id:
        await query.answer("این سفارش مربوط به شما نیست", show_alert=True)
        return

    if order["status"] != "pending":
        await query.edit_message_caption(caption=f"سفارش {order_id} قبلا {order['status']} شده است.")
        return

    await query.edit_message_caption(caption=f"در حال تایید سفارش {order_id}...")

    source_mode = str(order["subscription_source_mode"] or "xui").strip().lower()
    subscription_type = str(order["subscription_type"] or "normal").strip().lower()
    approved_successfully = False
    try:
        client_email = _build_client_email(order["referral_code"] or "ref", order_id)

        if source_mode == "file":
            claimed = db.claim_subscription_for_order(order_id, order_admin_id, subscription_type)
            if claimed is None:
                await query.edit_message_caption(caption=f"خطا: برای نوع {subscription_type_label(subscription_type)} اشتراک موجود نیست.")
                return
            vpn_link = str(claimed["config_link"])
            sub_link = str(claimed["sub_link"] or "").strip() or None
            base_price = int(claimed["price"] or order["base_price"] or default_price_for_type(subscription_type))
        else:
            inbound_id = int(os.getenv("XUI_INBOUND_ID", "1"))
            days_valid = int(os.getenv("XUI_DAYS_VALID", "30"))
            xui_port = int(os.getenv("XUI_PORT", "443"))
            public_host = os.getenv("XUI_PUBLIC_HOST")
            vpn_link = await asyncio.to_thread(
                xui_client.create_client,
                int(order["volume"]),
                inbound_id,
                days_valid,
                client_email,
                public_host,
                xui_port,
            )
            sub_link = None
            base_price = int(order["base_price"] or default_price_for_type(subscription_type))

        db.approve_order(order_id, vpn_link, sub_link)
        approved_successfully = True

        user_identity = str(order["user_id"])
        display_vpn_link = vpn_link if source_mode == "file" else _normalize_vless_link(vpn_link)
        final_caption = (
            f"✅ سفارش {order_id} تایید شد\n"
            f"👤 کاربر: {user_identity}\n"
            f"🆔 تلگرام: <code>{order['user_id']}</code>\n"
            f"🧩 نوع اشتراک: <b>{subscription_type_label(subscription_type)}</b>\n"
            f"💰 مبلغ پایه: <b>{_format_toman(base_price)}</b>\n"
            f"🎁 تخفیف: <b>{int(order['discount_percent'] or 0)}%</b>\n"
            f"💵 مبلغ نهایی: <b>{_format_toman(int(order['final_price'] or 0))}</b>\n"
            f"🔗 لینک کانفیگ:\n"
            f"<pre><code>{html.escape(display_vpn_link)}</code></pre>"
            f"\n🌐 لینک ساب:\n"
            f"<pre><code>{html.escape(sub_link or '-')}</code></pre>"
        )

        await context.bot.send_message(
            chat_id=order["user_id"],
            text=(
                "✅ <b>پرداخت شما تایید شد</b>\n\n"
                "🎉 اشتراک شما با موفقیت فعال شد.\n"
                f"🆔 شناسه سفارش: <code>{order_id}</code>\n"
                f"🏷️ کد معرف: <b>{html.escape(order['referral_code'] or '-')}</b>\n"
                f"🧩 نوع اشتراک: <b>{subscription_type_label(subscription_type)}</b>\n"
                # f"📧 ایمیل کلاینت: <code>{html.escape(client_email)}</code>\n"
                f"📦 حجم: <b>{order['volume']} گیگابایت</b>\n"
                f"💰 مبلغ نهایی: <b>{_format_toman(int(order['final_price'] or 0))}</b>\n\n"
                "🔗 <b>لینک کانفیگ:</b>\n"
                f"<pre><code>{html.escape(display_vpn_link)}</code></pre>\n"
                "🌐 <b>لینک ساب:</b>\n"
                f"<pre><code>{html.escape(sub_link or '-')}</code></pre>"
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_main_menu_keyboard(),
        )

        await context.bot.send_message(
            chat_id=order["user_id"],
            text="از دکمه «اشتراک ها» می‌توانید جزئیات کامل همه اشتراک‌ها را ببینید.",
            reply_markup=_main_menu_keyboard(),
        )

        await query.edit_message_caption(caption=final_caption, parse_mode="HTML")
    except XUIError as exc:
        if source_mode == "file" and not approved_successfully:
            db.release_subscription_for_order(order_id)
        logger.exception("خطای X-UI هنگام تایید سفارش")
        await query.edit_message_caption(
            caption=f"خطا در ساخت اکانت روی X-UI برای سفارش {order_id}: {exc}"
        )
    except Exception as exc:  # noqa: BLE001
        if source_mode == "file" and not approved_successfully:
            db.release_subscription_for_order(order_id)
        logger.exception("خطای غیرمنتظره هنگام تایید سفارش")
        await query.edit_message_caption(caption=f"سفارش {order_id} ناموفق بود: {exc}")


async def reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    query = update.callback_query
    await query.answer()

    if not _is_admin(query.from_user.id):
        await query.answer("اجازه دسترسی ندارید", show_alert=True)
        return

    data = query.data or ""
    if not data.startswith("reject:"):
        await query.answer("عملیات نامعتبر", show_alert=True)
        return

    order_id = data.split(":", 1)[1]
    order = db.get_order(order_id)
    if order is None:
        await query.edit_message_caption(caption="سفارش پیدا نشد.")
        return

    order_admin_id = int(order["admin_id"] or settings.admin_id)
    if query.from_user.id != order_admin_id:
        await query.answer("این سفارش مربوط به شما نیست", show_alert=True)
        return

    if order["status"] != "pending":
        await query.edit_message_caption(caption=f"سفارش {order_id} قبلا {order['status']} شده است.")
        return

    db.reject_order(order_id)
    await context.bot.send_message(
        chat_id=order["user_id"],
        text=(
            "❌ رسید پرداخت شما رد شد.\n"
            f"🆔 سفارش: {order_id}\n"
            "لطفا برای این سفارش دوباره رسید معتبر ارسال کنید یا خرید جدید ثبت کنید."
        ),
        reply_markup=_main_menu_keyboard(),
    )
    await query.edit_message_caption(caption=f"سفارش {order_id} رد شد ❌")


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("cancel:"):
        await query.answer("عملیات نامعتبر", show_alert=True)
        return

    order_id = data.split(":", 1)[1]
    order = db.get_order(order_id)
    if order is None:
        await query.answer("سفارش پیدا نشد", show_alert=True)
        return

    if int(order["user_id"]) != query.from_user.id:
        await query.answer("این سفارش متعلق به شما نیست", show_alert=True)
        return

    if order["status"] != "pending":
        await query.answer("این سفارش دیگر قابل لغو نیست", show_alert=True)
        return

    db.cancel_order(order_id)
    context.user_data["waiting_for_receipt"] = False
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id=order["user_id"],
        text=(
            "✅ سفارش شما لغو شد.\n"
            "اگر خواستید دوباره خرید کنید، از دکمه خرید استفاده کنید."
        ),
        reply_markup=_main_menu_keyboard(),
    )


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception in bot", exc_info=context.error)


def build_application() -> Application:
    app = Application.builder().token(settings.bot_token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("terms", terms))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("myaccount", myaccount))
    app.add_handler(CommandHandler("subscriptions", show_all_subscriptions))
    app.add_handler(CommandHandler("addrefcode", add_refcode))

    app.add_handler(CallbackQueryHandler(confirm_membership, pattern=r"^check_membership$"))
    app.add_handler(CallbackQueryHandler(accept_terms_callback, pattern=r"^accept_terms$"))
    app.add_handler(CallbackQueryHandler(subscription_type_callback, pattern=r"^subscription_type:"))
    app.add_handler(CallbackQueryHandler(approve_callback, pattern=r"^approve:"))
    app.add_handler(CallbackQueryHandler(reject_callback, pattern=r"^reject:"))
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^cancel:"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(handle_error)
    return app


def main() -> None:
    db.init_db()
    for admin_id in settings.admin_cards:
        db.ensure_admin_exists(admin_id)
        if db.get_admin_subscription_source_mode(admin_id) == "file":
            sync_subscription_inventory_from_file(admin_id, settings.subscription_file_path, Path(__file__).resolve().parent)
    app = build_application()
    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
