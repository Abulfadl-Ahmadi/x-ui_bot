import logging
from pathlib import Path

from telegram.error import BadRequest, Forbidden
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import db
from config import load_settings
from subscriptions import resolve_subscription_file_path, subscription_type_label, sync_subscription_inventory_from_file


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


settings = load_settings()


def _join_channel_keyboard() -> ReplyKeyboardMarkup:
    channel_url = settings.required_channel.lstrip("@")
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("عضویت در چنل", url=f"https://t.me/{channel_url}")],
            [InlineKeyboardButton("تایید عضویت ✅", callback_data="check_membership")],
        ]
    )


def _is_chat_member_joined(member: object) -> bool:
    status = getattr(member, "status", None)
    is_member = getattr(member, "is_member", None)
    return status in {"member", "administrator", "creator"} or bool(is_member)


async def _ensure_channel_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # Admin bot does not require channel membership.
    return True


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_cards


def _admin_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("وضعیت فروش"), KeyboardButton("باز کردن فروش")],
            [KeyboardButton("بستن فروش"), KeyboardButton("افزودن کد معرف")],
            [KeyboardButton("وضعیت فروش نوع اشتراک"), KeyboardButton("تنظیم فروش نوع اشتراک")],
            [KeyboardButton("افزودن کد تخفیف"), KeyboardButton("لیست کدهای معرف")],
            [KeyboardButton("لیست کدهای تخفیف"), KeyboardButton("باطل کردن کد تخفیف")],
            [KeyboardButton("حذف کد تخفیف")],
            [KeyboardButton("منبع XUI"), KeyboardButton("منبع فایل")],
            [KeyboardButton("همگام‌سازی اشتراک‌ها"), KeyboardButton("وضعیت منبع")],
            [KeyboardButton("رسیدهای در انتظار")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _clear_states(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["waiting_refcode"] = False
    context.user_data["waiting_discount_code"] = False
    context.user_data["waiting_discount_percent"] = False
    context.user_data["waiting_discount_max_uses"] = False
    context.user_data["waiting_discount_deactivate"] = False
    context.user_data["waiting_discount_delete"] = False
    context.user_data["waiting_subscription_sales_toggle"] = False


def _subscription_mode_text(mode: str) -> str:
    return "فایل" if str(mode).strip().lower() == "file" else "XUI"


def _subscription_type_title(subscription_type: str) -> str:
    return subscription_type_label("special" if str(subscription_type).strip().lower() == "special" else "normal")


def _parse_subscription_sales_toggle(text: str) -> tuple[bool, list[str]] | tuple[None, str]:
    normalized = str(text or "").strip().replace("،", " ").replace(",", " ")
    parts = [part for part in normalized.split() if part]
    if len(parts) < 2:
        return None, "فرمت نامعتبر است. مثال: خاموش معمولی ویژه"

    action = parts[0].lower()
    if action in {"روشن", "باز", "فعال", "on", "enable"}:
        is_open = True
    elif action in {"خاموش", "بسته", "غیرفعال", "off", "disable"}:
        is_open = False
    else:
        return None, "عملیات نامعتبر است. از «روشن» یا «خاموش» استفاده کنید."

    selected: list[str] = []
    seen: set[str] = set()
    for token in parts[1:]:
        key = token.lower().strip()
        if key in {"هر", "دو", "هردو", "همه", "all", "both"}:
            for sub_type in ("normal", "special"):
                if sub_type not in seen:
                    seen.add(sub_type)
                    selected.append(sub_type)
            continue

        mapped = None
        if key in {"معمولی", "normal", "n"}:
            mapped = "normal"
        elif key in {"ویژه", "special", "s"}:
            mapped = "special"

        if mapped is None:
            return None, f"نوع اشتراک نامعتبر است: {token}"

        if mapped not in seen:
            seen.add(mapped)
            selected.append(mapped)

    if not selected:
        return None, "حداقل یک نوع اشتراک مشخص کنید (معمولی/ویژه)."

    return is_open, selected


def _sync_subscription_inventory_for_admin(admin_id: int) -> tuple[str | None, int]:
    mode = db.get_admin_subscription_source_mode(admin_id)
    if mode != "file":
        return None, 0

    file_path = resolve_subscription_file_path(settings.subscription_file_path, Path(__file__).resolve().parent)
    if not file_path.exists():
        return str(file_path), 0

    synced_file, inserted = sync_subscription_inventory_from_file(
        admin_id,
        settings.subscription_file_path,
        Path(__file__).resolve().parent,
    )
    return str(synced_file), inserted


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.message.reply_text("این بات فقط مخصوص ادمین‌هاست.")
        return

    db.ensure_admin_exists(user_id)
    _clear_states(context)
    current_mode = _subscription_mode_text(db.get_admin_subscription_source_mode(user_id))
    await update.message.reply_text(
        f"پنل مدیریت ادمین فعال شد.\nمنبع فعلی اشتراک: {current_mode}",
        reply_markup=_admin_menu_keyboard(),
    )


async def confirm_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    query = update.callback_query
    if query:
        await query.answer("عضویت شما تایید شد ✅", show_alert=True)
        if query.message:
            await query.message.reply_text(
                "عضویت شما تایید شد ✅\nحالا از دکمه‌های پنل استفاده کنید.",
                reply_markup=_admin_menu_keyboard(),
            )


def _receipt_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Approve ✅", callback_data=f"approve:{order_id}"),
            InlineKeyboardButton("Reject ❌", callback_data=f"reject:{order_id}"),
        ]]
    )


async def _send_pending_receipts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.message.reply_text("این بات فقط مخصوص ادمین‌هاست.")
        return

    db.ensure_admin_exists(user_id)
    pending_orders = db.get_pending_orders_by_admin(user_id)
    if not pending_orders:
        await update.message.reply_text("هیچ رسید در انتظاری برای شما ثبت نشده است.")
        return

    for order in pending_orders:
        caption = (
            "🧾 <b>رسید در انتظار تایید</b>\n"
            f"🆔 سفارش: <code>{order['id']}</code>\n"
            f"👤 کاربر: <code>{order['user_id']}</code>\n"
            f"🏷️ کد معرف: <code>{order['referral_code'] or '-'}</code>\n"
            f"📦 حجم: <b>{order['volume']} گیگابایت</b>\n"
            f"💰 مبلغ نهایی: <b>{order['final_price'] or 0:,} تومان</b>"
        )

        if order["receipt_file_id"]:
            local_path = str(order["receipt_local_path"] or "").strip()
            if local_path and Path(local_path).exists():
                with Path(local_path).open("rb") as image_file:
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=image_file,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=_receipt_keyboard(order["id"]),
                    )
            else:
                try:
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=order["receipt_file_id"],
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=_receipt_keyboard(order["id"]),
                    )
                except BadRequest:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=caption + "\n\n⚠️ عکس رسید در دسترس این بات نیست.",
                        parse_mode="HTML",
                        reply_markup=_receipt_keyboard(order["id"]),
                    )
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text=caption,
                parse_mode="HTML",
                reply_markup=_receipt_keyboard(order["id"]),
            )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    if not _is_admin(user_id):
        await update.message.reply_text("این بات فقط مخصوص ادمین‌هاست.")
        return

    db.ensure_admin_exists(user_id)

    if text == "وضعیت فروش":
        status = "باز" if db.is_admin_sales_open(user_id) else "بسته"
        await update.message.reply_text(f"وضعیت فروش شما: {status}")
        return

    if text == "باز کردن فروش":
        db.set_admin_sales_open(user_id, True)
        await update.message.reply_text("فروش شما باز شد ✅")
        return

    if text == "بستن فروش":
        db.set_admin_sales_open(user_id, False)
        await update.message.reply_text("فروش شما بسته شد ⛔")
        return

    if text == "وضعیت فروش نوع اشتراک":
        sales_status = db.get_admin_subscription_sales_status(user_id)
        normal_status = "باز ✅" if sales_status.get("normal", True) else "تکمیل ظرفیت ⛔"
        special_status = "باز ✅" if sales_status.get("special", True) else "تکمیل ظرفیت ⛔"
        await update.message.reply_text(
            "وضعیت فروش نوع اشتراک:\n"
            f"- {_subscription_type_title('normal')}: {normal_status}\n"
            f"- {_subscription_type_title('special')}: {special_status}",
            reply_markup=_admin_menu_keyboard(),
        )
        return

    if text == "تنظیم فروش نوع اشتراک":
        _clear_states(context)
        context.user_data["waiting_subscription_sales_toggle"] = True
        await update.message.reply_text(
            "دستور را به این شکل ارسال کنید:\n"
            "روشن معمولی\n"
            "خاموش ویژه\n"
            "خاموش معمولی ویژه",
            reply_markup=_admin_menu_keyboard(),
        )
        return

    if text == "افزودن کد معرف":
        _clear_states(context)
        context.user_data["waiting_refcode"] = True
        await update.message.reply_text("کدهای معرف را بفرستید (با فاصله یا ویرگول جدا کنید).")
        return

    if text == "افزودن کد تخفیف":
        _clear_states(context)
        context.user_data["waiting_discount_code"] = True
        await update.message.reply_text("کد تخفیف را ارسال کنید.")
        return

    if text == "لیست کدهای معرف":
        codes = db.list_referral_codes()
        owner_codes = []
        for code in codes:
            owner = db.get_referral_code_owner(code)
            if owner == user_id:
                owner_codes.append(code)

        if not owner_codes:
            await update.message.reply_text("هنوز کد معرفی برای شما ثبت نشده است.")
            return

        message = "کدهای معرف شما:\n" + "\n".join(f"- {code}" for code in owner_codes)
        await update.message.reply_text(message)
        return

    if text == "لیست کدهای تخفیف":
        rows = db.list_discount_codes(user_id)
        if not rows:
            await update.message.reply_text("هنوز کد تخفیفی برای شما ثبت نشده است.")
            return

        lines = ["کدهای تخفیف شما:"]
        for row in rows:
            status = "فعال" if int(row["is_active"]) == 1 else "غیرفعال"
            lines.append(
                f"- {row['code']} | {row['percent']}% | {row['used_count']}/{row['max_uses']} | {status}"
            )
        await update.message.reply_text("\n".join(lines))
        return

    if text == "باطل کردن کد تخفیف":
        _clear_states(context)
        context.user_data["waiting_discount_deactivate"] = True
        await update.message.reply_text("کد تخفیفی که می‌خواهید باطل شود را ارسال کنید.")
        return

    if text == "حذف کد تخفیف":
        _clear_states(context)
        context.user_data["waiting_discount_delete"] = True
        await update.message.reply_text("کد تخفیفی که می‌خواهید حذف شود را ارسال کنید.")
        return

    if text == "وضعیت منبع":
        mode = db.get_admin_subscription_source_mode(user_id)
        message = [f"منبع فعلی اشتراک: {_subscription_mode_text(mode)}"]
        if mode == "file":
            file_path, inserted = _sync_subscription_inventory_for_admin(user_id)
            message.append(f"فایل: {file_path or settings.subscription_file_path}")
            message.append(f"همگام‌سازی اخیر: {inserted} مورد")
        await update.message.reply_text("\n".join(message), reply_markup=_admin_menu_keyboard())
        return

    if text == "منبع فایل":
        db.set_admin_subscription_source_mode(user_id, "file")
        file_path, inserted = _sync_subscription_inventory_for_admin(user_id)
        if file_path is None:
            await update.message.reply_text(
                "منبع اشتراک روی فایل تنظیم شد، اما فایل قابل‌خواندن نبود.",
                reply_markup=_admin_menu_keyboard(),
            )
            return
        await update.message.reply_text(
            f"منبع اشتراک روی فایل تنظیم شد ✅\nفایل: {file_path}\nهمگام‌سازی شد: {inserted} مورد",
            reply_markup=_admin_menu_keyboard(),
        )
        return

    if text == "منبع XUI":
        db.set_admin_subscription_source_mode(user_id, "xui")
        await update.message.reply_text(
            "منبع اشتراک روی XUI تنظیم شد ✅",
            reply_markup=_admin_menu_keyboard(),
        )
        return

    if text == "همگام‌سازی اشتراک‌ها":
        mode = db.get_admin_subscription_source_mode(user_id)
        if mode != "file":
            await update.message.reply_text("این ادمین روی XUI تنظیم شده است و همگام‌سازی فایل لازم نیست.")
            return
        file_path, inserted = _sync_subscription_inventory_for_admin(user_id)
        await update.message.reply_text(
            f"همگام‌سازی انجام شد.\nفایل: {file_path or settings.subscription_file_path}\nتعداد پردازش‌شده: {inserted}",
            reply_markup=_admin_menu_keyboard(),
        )
        return

    if text == "رسیدهای در انتظار":
        await _send_pending_receipts(update, context)
        return

    if context.user_data.get("waiting_refcode"):
        raw_codes = text.replace(",", " ").split()
        codes = [c.strip() for c in raw_codes if c.strip()]
        if not codes:
            await update.message.reply_text("کد معتبر وارد نشد. دوباره ارسال کنید.")
            return

        inserted = db.add_referral_codes(codes, user_id)
        skipped = len(codes) - inserted
        _clear_states(context)
        await update.message.reply_text(
            f"انجام شد.\nافزوده شد: {inserted}\nتکراری: {skipped}",
            reply_markup=_admin_menu_keyboard(),
        )
        return

    if context.user_data.get("waiting_discount_code"):
        if not text:
            await update.message.reply_text("کد تخفیف نمی‌تواند خالی باشد.")
            return
        context.user_data["draft_discount_code"] = text
        context.user_data["waiting_discount_code"] = False
        context.user_data["waiting_discount_percent"] = True
        await update.message.reply_text("درصد تخفیف را وارد کنید (عدد بین 1 تا 100).")
        return

    if context.user_data.get("waiting_discount_percent"):
        if not text.isdigit():
            await update.message.reply_text("درصد باید عدد باشد.")
            return
        percent = int(text)
        if percent <= 0 or percent > 100:
            await update.message.reply_text("درصد باید بین 1 تا 100 باشد.")
            return

        context.user_data["draft_discount_percent"] = percent
        context.user_data["waiting_discount_percent"] = False
        context.user_data["waiting_discount_max_uses"] = True
        await update.message.reply_text("حداکثر تعداد استفاده را وارد کنید (مثلا 20).")
        return

    if context.user_data.get("waiting_discount_max_uses"):
        if not text.isdigit():
            await update.message.reply_text("تعداد استفاده باید عدد باشد.")
            return
        max_uses = int(text)
        if max_uses <= 0:
            await update.message.reply_text("تعداد استفاده باید بیشتر از صفر باشد.")
            return

        code = str(context.user_data.get("draft_discount_code", "")).strip()
        percent = int(context.user_data.get("draft_discount_percent", 0))
        if not code or percent <= 0:
            _clear_states(context)
            await update.message.reply_text("فرآیند ناقص شد. دوباره تلاش کنید.")
            return

        created = db.create_discount_code(code, user_id, percent, max_uses)
        _clear_states(context)
        context.user_data.pop("draft_discount_code", None)
        context.user_data.pop("draft_discount_percent", None)

        if not created:
            await update.message.reply_text(
                "این کد از قبل وجود دارد. یک کد دیگر انتخاب کنید.",
                reply_markup=_admin_menu_keyboard(),
            )
            return

        await update.message.reply_text(
            f"کد تخفیف ساخته شد ✅\nکد: {code}\nدرصد: {percent}%\nحداکثر استفاده: {max_uses}",
            reply_markup=_admin_menu_keyboard(),
        )
        return

    if context.user_data.get("waiting_discount_deactivate"):
        code = text.strip()
        if not code:
            await update.message.reply_text("کد تخفیف نمی‌تواند خالی باشد.")
            return

        result = db.deactivate_discount_code(code, user_id)
        _clear_states(context)

        if result == "not_found":
            await update.message.reply_text(
                "این کد برای شما پیدا نشد.",
                reply_markup=_admin_menu_keyboard(),
            )
            return

        if result == "already_inactive":
            await update.message.reply_text(
                "این کد از قبل غیرفعال بوده است.",
                reply_markup=_admin_menu_keyboard(),
            )
            return

        await update.message.reply_text(
            f"کد تخفیف {code} باطل شد ✅",
            reply_markup=_admin_menu_keyboard(),
        )
        return

    if context.user_data.get("waiting_discount_delete"):
        code = text.strip()
        if not code:
            await update.message.reply_text("کد تخفیف نمی‌تواند خالی باشد.")
            return

        deleted = db.delete_discount_code(code, user_id)
        _clear_states(context)

        if not deleted:
            await update.message.reply_text(
                "این کد برای شما پیدا نشد.",
                reply_markup=_admin_menu_keyboard(),
            )
            return

        await update.message.reply_text(
            f"کد تخفیف {code} حذف شد 🗑️",
            reply_markup=_admin_menu_keyboard(),
        )
        return

    if context.user_data.get("waiting_subscription_sales_toggle"):
        parsed = _parse_subscription_sales_toggle(text)
        if parsed[0] is None:
            await update.message.reply_text(str(parsed[1]))
            return

        is_open = bool(parsed[0])
        selected_types = list(parsed[1])
        for subscription_type in selected_types:
            db.set_admin_subscription_type_sales_open(user_id, subscription_type, is_open)

        _clear_states(context)
        state_text = "باز ✅" if is_open else "تکمیل ظرفیت ⛔"
        changed_types = "، ".join(_subscription_type_title(subscription_type) for subscription_type in selected_types)
        await update.message.reply_text(
            f"وضعیت فروش برای {changed_types} تنظیم شد: {state_text}",
            reply_markup=_admin_menu_keyboard(),
        )
        return

    await update.message.reply_text("از دکمه‌های پنل استفاده کنید.", reply_markup=_admin_menu_keyboard())


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception in admin bot", exc_info=context.error)


async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    query = update.callback_query
    await query.answer()

    if not _is_admin(query.from_user.id):
        await query.answer("این بات فقط مخصوص ادمین‌هاست.", show_alert=True)
        return

    data = query.data or ""
    if not data.startswith("approve:"):
        await query.answer("عملیات نامعتبر", show_alert=True)
        return

    order_id = data.split(":", 1)[1]
    order = db.get_order(order_id)
    if order is None:
        await query.answer("سفارش پیدا نشد", show_alert=True)
        return

    if int(order["admin_id"] or 0) != query.from_user.id:
        await query.answer("این سفارش مربوط به شما نیست", show_alert=True)
        return

    if order["status"] != "pending":
        await query.answer("این سفارش قبلا بررسی شده است", show_alert=True)
        return

    await query.edit_message_caption(caption="در حال تایید سفارش...")

    try:
        import asyncio
        import os

        from xui import XUIClient

        client = XUIClient(
            base_url=settings.xui_url,
            username=settings.xui_username,
            password=settings.xui_password,
        )
        inbound_id = int(os.getenv("XUI_INBOUND_ID", "1"))
        days_valid = int(os.getenv("XUI_DAYS_VALID", "30"))
        xui_port = int(os.getenv("XUI_PORT", "443"))
        public_host = os.getenv("XUI_PUBLIC_HOST")

        client_email = f"{str(order['referral_code'] or 'ref')[:6]}-{order_id.replace('-', '')[:6]}"
        vpn_link = await asyncio.to_thread(
            client.create_client,
            int(order["volume"]),
            inbound_id,
            days_valid,
            client_email,
            public_host,
            xui_port,
        )

        db.approve_order(order_id, vpn_link)
        await context.bot.send_message(
            chat_id=int(order["user_id"]),
            text=f"✅ سفارش شما تایید شد.\n\n{vpn_link}",
            disable_web_page_preview=True,
        )
        await query.edit_message_caption(caption=f"سفارش {order_id} تایید شد ✅")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Approval failed in admin bot")
        await query.edit_message_caption(caption=f"خطا در تایید سفارش: {exc}")


async def reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_channel_membership(update, context):
        return

    query = update.callback_query
    await query.answer()

    if not _is_admin(query.from_user.id):
        await query.answer("این بات فقط مخصوص ادمین‌هاست.", show_alert=True)
        return

    data = query.data or ""
    if not data.startswith("reject:"):
        await query.answer("عملیات نامعتبر", show_alert=True)
        return

    order_id = data.split(":", 1)[1]
    order = db.get_order(order_id)
    if order is None:
        await query.answer("سفارش پیدا نشد", show_alert=True)
        return

    if int(order["admin_id"] or 0) != query.from_user.id:
        await query.answer("این سفارش مربوط به شما نیست", show_alert=True)
        return

    if order["status"] != "pending":
        await query.answer("این سفارش قبلا بررسی شده است", show_alert=True)
        return

    db.reject_order(order_id)
    await context.bot.send_message(
        chat_id=int(order["user_id"]),
        text=(
            "❌ سفارش شما رد شد.\n"
            f"🆔 سفارش: {order_id}\n"
            "برای ادامه، خرید جدید ثبت کنید یا با پشتیبانی تماس بگیرید."
        ),
    )
    await query.edit_message_caption(caption=f"سفارش {order_id} رد شد ❌")


def build_application() -> Application:
    if not settings.admin_bot_token:
        raise ValueError("Missing ADMIN_BOT_TOKEN in environment")

    app = Application.builder().token(settings.admin_bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(confirm_membership, pattern=r"^check_membership$"))
    app.add_handler(CallbackQueryHandler(approve_callback, pattern=r"^approve:"))
    app.add_handler(CallbackQueryHandler(reject_callback, pattern=r"^reject:"))
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
    logger.info("Admin bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
