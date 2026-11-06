"""Telegram bot: helpers + long-polling loop + commands."""
import os
import re
import logging
import time

import requests

log = logging.getLogger("bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9203674740:AAGmZH3goPdV8ADaC4Pxnshnn6gN2YDnc0w").strip()
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

BOT_USERNAME = "trackingexpressvnbot"


def is_enabled():
    return bool(BOT_TOKEN)


def get_bot_username():
    return BOT_USERNAME


def _post(method, **params):
    if not BOT_TOKEN:
        return None
    try:
        r = requests.post(f"{API_BASE}/{method}", json=params, timeout=30)
        return r.json()
    except Exception as e:
        log.warning("telegram %s: %s", method, e)
        return None


def _get(method, **params):
    if not BOT_TOKEN:
        return None
    try:
        r = requests.get(f"{API_BASE}/{method}", params=params, timeout=40)
        return r.json()
    except Exception as e:
        log.warning("telegram %s: %s", method, e)
        return None


def send_message(chat_id, text, **kwargs):
    return _post("sendMessage", chat_id=chat_id, text=text,
                 parse_mode="HTML", disable_web_page_preview=True, **kwargs)


def _esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ================= Callbacks =================
_CB = {}


def setup(**callbacks):
    _CB.update(callbacks)


def _call(name, *args, **kwargs):
    fn = _CB.get(name)
    if not fn:
        log.warning("bot callback missing: %s", name)
        return None
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log.exception("callback %s lỗi: %s", name, e)
        return None

# ================= Format =================
def _format_status(t):
    code = _esc(t.get("tracking_code"))
    cname = _esc(t.get("carrier_name") or t.get("carrier"))
    status = _esc(t.get("last_status") or "—")
    product = _esc(t.get("product_name") or "—")
    location = _esc(t.get("last_location") or "")
    next_loc = _esc(t.get("last_next_loc") or "")
    ev_time = _esc(t.get("last_event_time") or t.get("last_checked_at") or "—")
    note = _esc(t.get("note") or "")
    delivered = "✅" if t.get("delivered_at") else "🚚"

    lines = [
        f"{delivered} <code>{code}</code> — {cname}",
        f"📝 Trạng thái: <b>{status}</b>",
    ]
    if product != "—":
        lines.append(f"📦 Sản phẩm: {product}")
    if location:
        lines.append(f"📍 Vị trí: {location}")
    if next_loc:
        lines.append(f"➡️ Tiếp theo: {next_loc}")
    lines.append(f"🕒 {ev_time}")
    if note:
        lines.append(f"🗒 Ghi chú: {note}")
    return "\n".join(lines)


# ================= Login link =================
def _send_login_link(chat_id, user_id):
    code = _call("create_login_code", user_id)
    web_url = (_call("get_web_url") or "").rstrip("/")
    ttl = _call("get_login_ttl") or 5

    if not code:
        send_message(chat_id, "❌ Không tạo được mã. Thử lại sau.")
        return

    if not web_url:
        send_message(
            chat_id,
            f"🔐 <b>Mã đăng nhập web</b>\n\n<code>{code}</code>\n\n"
            f"Hiệu lực {ttl} phút. Mở web và nhập mã này.",
        )
        return

    login_url = f"{web_url}/?code={code}"
    text = (
        f"🔐 <b>Đăng nhập web</b>\n\n"
        f"Hiệu lực {ttl} phút. Nhấn nút bên dưới để đăng nhập tự động:"
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "🚀 Mở web & đăng nhập", "url": login_url}
        ]]
    }
    send_message(chat_id, text, reply_markup=keyboard)


# ================= Commands =================
def cmd_start(chat_id, user_id, args):
    if args and args[0].lower() == "login":
        return _send_login_link(chat_id, user_id)

    name = _call("get_user_name", user_id) or "bạn"
    web_url = (_call("get_web_url") or "").rstrip("/")
    login_btn = []
    if web_url:
        login_btn = [[{"text": "🌐 Đăng nhập web", "url": f"{web_url}/?start=1"}]]

    text = (
        f"👋 Chào <b>{_esc(name)}</b>!\n\n"
        "Bot theo dõi vận đơn đa hãng.\n\n"
        "<b>Lệnh:</b>\n"
        "• /login — Lấy link đăng nhập web\n"
        "• /add &lt;code&gt; [sdt] — Thêm đơn\n"
        "• /list — Danh sách đơn\n"
        "• /check &lt;code&gt; — Làm mới 1 đơn\n"
        "• /detail &lt;code&gt; — Hành trình\n"
        "• /del &lt;code&gt; — Xóa đơn\n"
        "• /noti on|off — Bật/tắt thông báo\n"
        "• /me — Thông tin tài khoản\n"
        "• /help — Trợ giúp\n\n"
        "💡 Mẹo: bạn có thể gửi thẳng mã vận đơn vào chat, bot sẽ thử thêm."
    )
    kwargs = {}
    if login_btn:
        kwargs["reply_markup"] = {"inline_keyboard": login_btn}
    send_message(chat_id, text, **kwargs)


def cmd_help(chat_id, user_id, args):
    cmd_start(chat_id, user_id, args)


def cmd_login(chat_id, user_id, args):
    _send_login_link(chat_id, user_id)


def cmd_me(chat_id, user_id, args):
    info = _call("bot_user_info", user_id) or {}
    noti = "🔔 bật" if info.get("notifications_enabled") else "🔕 tắt"
    lines = [
        "👤 <b>Tài khoản</b>",
        f"• ID: <code>{user_id}</code>",
        f"• Tên: {_esc(info.get('first_name') or '')}",
        f"• Username: {('@'+info['username']) if info.get('username') else '(không có)'}",
        f"• Vai trò: {'Admin' if info.get('is_admin') else 'User'}",
        f"• Thông báo: {noti}",
        f"• Số đơn: {info.get('tracking_count', 0)}",
    ]
    send_message(chat_id, "\n".join(lines))


def cmd_add(chat_id, user_id, args):
    if not args:
        send_message(chat_id, "❌ Cú pháp: <code>/add MÃ_VẬN_ĐƠN [SĐT]</code>")
        return
    code = args[0].strip().upper()
    phone = args[1].strip() if len(args) > 1 else None
    r = _call("bot_add_tracking", user_id, code, phone)
    if not r:
        send_message(chat_id, "❌ Không thêm được đơn.")
        return
    if r.get("error"):
        if r.get("need_phone"):
            send_message(chat_id,
                f"📱 Cần 4 số cuối SĐT. Gửi lại:\n<code>/add {_esc(code)} 1234</code>")
        else:
            send_message(chat_id, f"❌ {_esc(r['error'])}")
        return
    t = r.get("tracking")
    if t:
        send_message(chat_id, "✅ Đã thêm đơn:\n\n" + _format_status(t))
    else:
        send_message(chat_id, "✅ Đã thêm đơn.")


def cmd_list(chat_id, user_id, args):
    rows = _call("bot_list_trackings", user_id) or []
    if not rows:
        send_message(chat_id, "📭 Chưa có đơn nào. Dùng /add để thêm.")
        return
    lines = [f"📋 <b>{len(rows)} đơn</b>\n"]
    for t in rows[:40]:
        icon = "✅" if t.get("delivered_at") else ("🚚" if t.get("is_active") else "⏸")
        lines.append(
            f"{icon} <code>{_esc(t['tracking_code'])}</code> — "
            f"{_esc(t.get('last_status') or '?')}"
        )
    if len(rows) > 40:
        lines.append(f"… và {len(rows) - 40} đơn khác")
    send_message(chat_id, "\n".join(lines))


def cmd_check(chat_id, user_id, args):
    if not args:
        send_message(chat_id, "❌ Cú pháp: <code>/check MÃ_VẬN_ĐƠN</code>")
        return
    r = _call("bot_refresh_tracking", user_id, args[0].strip().upper())
    if not r:
        send_message(chat_id, "❌ Không tìm thấy đơn.")
        return
    if r.get("error"):
        send_message(chat_id, f"❌ {_esc(r['error'])}")
        return
    t = r.get("tracking")
    if t:
        send_message(chat_id, _format_status(t))


def cmd_detail(chat_id, user_id, args):
    if not args:
        send_message(chat_id, "❌ Cú pháp: <code>/detail MÃ_VẬN_ĐƠN</code>")
        return
    r = _call("bot_detail_tracking", user_id, args[0].strip().upper())
    if not r:
        send_message(chat_id, "❌ Không tìm thấy đơn.")
        return
    if r.get("error"):
        send_message(chat_id, f"❌ {_esc(r['error'])}")
        return
    t = r.get("tracking") or {}
    events = r.get("events") or []
    lines = [_format_status(t), ""]
    if events:
        lines.append(f"🗺 <b>Hành trình ({len(events)}):</b>")
        for ev in reversed(events[-15:]):
            when = ev.get("time") or "—"
            st = ev.get("status") or ev.get("description") or ""
            loc = ev.get("location") or ""
            line = f"• {_esc(when)} — {_esc(st)}"
            if loc:
                line += f"\n  📍 {_esc(loc)}"
            lines.append(line)
    else:
        lines.append("(chưa có hành trình)")
    send_message(chat_id, "\n".join(lines))


def cmd_del(chat_id, user_id, args):
    if not args:
        send_message(chat_id, "❌ Cú pháp: <code>/del MÃ_VẬN_ĐƠN</code>")
        return
    ok = _call("bot_delete_tracking", user_id, args[0].strip().upper())
    send_message(chat_id, "🗑 Đã xóa đơn." if ok else "❌ Không tìm thấy đơn.")


def cmd_noti(chat_id, user_id, args):
    if not args or args[0].lower() not in ("on", "off"):
        send_message(chat_id, "❌ Cú pháp: <code>/noti on|off</code>")
        return
    enabled = args[0].lower() == "on"
    _call("bot_set_notifications", user_id, enabled)
    send_message(chat_id, "🔔 Đã bật thông báo." if enabled else "🔕 Đã tắt thông báo.")


def cmd_users(chat_id, user_id, args):
    if not _call("is_admin", user_id):
        send_message(chat_id, "⛔ Chỉ admin.")
        return
    rows = _call("bot_list_users") or []
    lines = [f"👥 <b>{len(rows)} người dùng</b>\n"]
    for r in rows[:50]:
        lines.append(
            f"• <code>{r['id']}</code> {_esc(r.get('first_name') or '')} "
            f"({'admin' if r.get('is_admin') else 'user'}) — "
            f"{r.get('tracking_count', 0)} đơn"
        )
    send_message(chat_id, "\n".join(lines))


def cmd_stats(chat_id, user_id, args):
    if not _call("is_admin", user_id):
        send_message(chat_id, "⛔ Chỉ admin.")
        return
    s = _call("bot_stats") or {}
    send_message(chat_id,
        f"📊 <b>Thống kê</b>\n"
        f"👥 Users: {s.get('users', 0)}\n"
        f"📦 Tổng đơn: {s.get('total', 0)}\n"
        f"🚚 Đang giao: {s.get('active', 0)}\n"
        f"✅ Đã giao: {s.get('delivered', 0)}")


def cmd_broadcast(chat_id, user_id, args):
    if not _call("is_admin", user_id):
        send_message(chat_id, "⛔ Chỉ admin.")
        return
    if not args:
        send_message(chat_id, "❌ Cú pháp: <code>/broadcast Nội dung</code>")
        return
    n = _call("bot_broadcast", " ".join(args)) or 0
    send_message(chat_id, f"📢 Đã gửi tới {n} người.")


COMMANDS = {
    "/start": cmd_start, "/help": cmd_help, "/login": cmd_login, "/me": cmd_me,
    "/add": cmd_add, "/list": cmd_list, "/check": cmd_check,
    "/detail": cmd_detail, "/del": cmd_del, "/noti": cmd_noti,
    "/users": cmd_users, "/stats": cmd_stats, "/broadcast": cmd_broadcast,
}

# ================= Plain text =================
_CODE_RE = re.compile(r"^[A-Z0-9]{5,40}$")


def _handle_text(chat_id, user_id, text):
    text = text.strip().upper()
    if not _CODE_RE.match(text):
        return
    r = _call("bot_add_tracking", user_id, text, None)
    if not r:
        return
    if r.get("error") and not r.get("need_phone"):
        d = _call("bot_detail_tracking", user_id, text)
        if d and d.get("tracking"):
            send_message(chat_id, _format_status(d["tracking"]))
        else:
            send_message(chat_id, f"❌ {_esc(r['error'])}")
        return
    if r.get("need_phone"):
        send_message(chat_id,
            f"📱 Đơn <code>{_esc(text)}</code> cần 4 số cuối SĐT.\n"
            f"Gửi: <code>/add {_esc(text)} 1234</code>")
        return
    t = r.get("tracking")
    if t:
        send_message(chat_id, "✅ Đã thêm:\n\n" + _format_status(t))

# ================= Update loop =================
def _handle_update(upd):
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    chat_id = (msg.get("chat") or {}).get("id")
    user = msg.get("from") or {}
    user_id = user.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not user_id:
        return

    _call("register_user", user)

    if not text:
        return

    if text.startswith("/"):
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        fn = COMMANDS.get(cmd)
        if fn:
            fn(chat_id, user_id, args)
        else:
            send_message(chat_id, "❓ Lệnh không hợp lệ. Gõ /help.")
        return

    _handle_text(chat_id, user_id, text)


def poll_loop():
    global BOT_USERNAME
    if not BOT_TOKEN:
        log.info("TELEGRAM_BOT_TOKEN chưa cấu hình → bot tắt")
        return

    me = _get("getMe")
    if not me or not me.get("ok"):
        log.error("getMe thất bại: %s", me)
        return
    BOT_USERNAME = (me.get("result") or {}).get("username")
    log.info("Bot online: @%s", BOT_USERNAME)

    offset = None
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=35)
            data = r.json()
            if not data.get("ok"):
                time.sleep(5)
                continue
            for upd in data.get("result") or []:
                offset = upd["update_id"] + 1
                try:
                    _handle_update(upd)
                except Exception as e:
                    log.exception("handle_update: %s", e)
        except Exception as e:
            log.warning("poll_loop: %s", e)
            time.sleep(3)
