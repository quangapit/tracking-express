import os
import json
import time
import secrets
import sqlite3
import logging
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from flask import (
    Flask, render_template, request, jsonify, g, session, redirect, url_for
)

import track

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tracker")

app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=30)

# ============================================================
# CONFIG
# ============================================================
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL_SECONDS", "600"))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600"))
DELETE_AFTER_DELIVERED_DAYS = int(os.getenv("DELETE_AFTER_DELIVERED_DAYS", "7"))

DB_DIR = os.getenv("DB_DIR", "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "tracker.db")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9203674740:AAGmZH3goPdV8ADaC4Pxnshnn6gN2YDnc0w").strip()
WEB_BASE_URL = (os.getenv("WEB_BASE_URL")
                or os.getenv("RENDER_EXTERNAL_URL")
                or "https://trackingexpressvn.onrender.com").rstrip("/")

# ============================================================
# DB helpers — WAL + busy_timeout + write lock
# ============================================================
_write_lock = threading.RLock()   # serialize mọi thao tác ghi

def _db_connect() -> sqlite3.Connection:
    """Mở connection với PRAGMA tối ưu tương tranh."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")   # retry 30s khi bị lock
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = _db_connect()
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    with closing(_db_connect()) as db:
        # WAL: đọc song song với ghi, giảm contention
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA wal_autocheckpoint = 1000")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id  INTEGER UNIQUE,
                name         TEXT,
                login_token  TEXT UNIQUE NOT NULL,
                created_at   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trackings (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                carrier          TEXT NOT NULL,
                tracking_code    TEXT NOT NULL,
                phone            TEXT,
                note             TEXT,
                product_name     TEXT,
                last_status      TEXT,
                last_status_code TEXT,
                last_phase       TEXT,
                last_location    TEXT,
                last_next_loc    TEXT,
                last_event_time  TEXT,
                last_events      TEXT,
                last_payload     TEXT,
                last_checked_at  TEXT,
                delivered_at     TEXT,
                is_active        INTEGER NOT NULL DEFAULT 1,
                created_at       TEXT NOT NULL,
                UNIQUE(user_id, carrier, tracking_code)
            );
            CREATE INDEX IF NOT EXISTS idx_trackings_active    ON trackings(is_active);
            CREATE INDEX IF NOT EXISTS idx_trackings_user      ON trackings(user_id);
            CREATE INDEX IF NOT EXISTS idx_trackings_delivered ON trackings(delivered_at);
        """)
        cols = {r[1] for r in db.execute("PRAGMA table_info(trackings)").fetchall()}
        for col, typ in [
            ("phone", "TEXT"),
            ("last_status_code", "TEXT"),
            ("last_phase", "TEXT"),
            ("last_location", "TEXT"),
            ("last_next_loc", "TEXT"),
            ("last_event_time", "TEXT"),
            ("last_events", "TEXT"),
            ("last_payload", "TEXT"),
        ]:
            if col not in cols:
                db.execute(f"ALTER TABLE trackings ADD COLUMN {col} {typ}")
        db.commit()
    log.info("DB ready: %s (WAL)", DB_PATH)

def _new_token(db):
    while True:
        t = str(secrets.randbelow(900000) + 100000)
        if not db.execute("SELECT 1 FROM users WHERE login_token = ?", (t,)).fetchone():
            return t

def get_or_create_user(telegram_id, name):
    with _write_lock, closing(_db_connect()) as db:
        row = db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if row:
            if name and row["name"] != name:
                db.execute("UPDATE users SET name = ? WHERE id = ?", (name, row["id"]))
                db.commit()
                row = db.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
            return dict(row)
        token = _new_token(db)
        now = datetime.now(timezone.utc).isoformat()
        cur = db.execute(
            "INSERT INTO users (telegram_id, name, login_token, created_at) VALUES (?,?,?,?)",
            (telegram_id, name, token, now),
        )
        db.commit()
        return dict(db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())

# ============================================================
# AUTH
# ============================================================
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        session.clear()
        return None
    return row

def require_user(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            return jsonify({"error": "Chưa đăng nhập"}), 401
        g.user = u
        return f(*args, **kwargs)
    return wrapper

# ============================================================
# Business
# ============================================================
def refresh_one(row):
    if not row["is_active"]:
        return {"status": row["last_status"], "skipped": True}

    carrier, code = row["carrier"], row["tracking_code"]
    phone = row["phone"] if "phone" in row.keys() else None

    try:
        data = track.call(carrier, code, phone)
    except Exception as e:
        log.warning("refresh %s/%s lỗi: %s", carrier, code, e)
        return {"error": str(e)}

    if isinstance(data, dict) and data.get("need_phone") and not phone:
        return {"error": "Cần 4 số cuối SĐT"}

    info = track.extract_all(data)
    now = datetime.now(timezone.utc).isoformat()

    # Ghi dưới write lock — tránh "database is locked"
    with _write_lock:
        db = get_db()
        db.execute(
            """
            UPDATE trackings
               SET last_status      = ?,
                   last_status_code = COALESCE(?, last_status_code),
                   last_phase       = COALESCE(?, last_phase),
                   last_location    = COALESCE(?, last_location),
                   last_next_loc    = COALESCE(?, last_next_loc),
                   last_event_time  = COALESCE(?, last_event_time),
                   product_name     = COALESCE(?, product_name),
                   last_events      = ?,
                   last_payload     = ?,
                   last_checked_at  = ?,
                   delivered_at     = CASE WHEN ? THEN COALESCE(delivered_at, ?) ELSE delivered_at END,
                   is_active        = CASE WHEN ? THEN 0 ELSE is_active END
             WHERE id = ?
            """,
            (info["status"] or "unknown",
             info["status_code"], info["phase"], info["location"],
             info["next_location"], info["event_time"], info["product"],
             json.dumps(info["events"], ensure_ascii=False),
             json.dumps(data, ensure_ascii=False)[:200_000],
             now, info["delivered"], now, info["delivered"], row["id"]),
        )
        db.commit()
    return {
        "status": info["status"],
        "status_code": info["status_code"],
        "product": info["product"],
        "events_count": len(info["events"]),
        "delivered": info["delivered"],
    }

def cleanup_old():
    """CHỈ gọi từ background — không gọi từ request GET nữa."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=DELETE_AFTER_DELIVERED_DAYS)).isoformat()
    with _write_lock:
        with closing(_db_connect()) as db:
            cur = db.execute(
                "DELETE FROM trackings WHERE delivered_at IS NOT NULL AND delivered_at < ?",
                (cutoff,),
            )
            db.commit()
            if cur.rowcount:
                log.info("cleanup: xóa %d bản ghi", cur.rowcount)

def background_loop():
    last_cleanup = 0.0
    while True:
        try:
            with app.app_context():
                db = get_db()
                # Đọc danh sách (không lock)
                rows = db.execute(
                    "SELECT * FROM trackings WHERE is_active = 1 AND user_id IS NOT NULL"
                ).fetchall()
                log.info("background: refresh %d bản ghi", len(rows))
                # Đóng connection đọc trước khi refresh (refresh_one tự mở lại qua g.db)
                close_db(None)

                for row in rows:
                    with app.app_context():
                        refresh_one(row)

                now_ts = time.time()
                if now_ts - last_cleanup > CLEANUP_INTERVAL:
                    cleanup_old()
                    last_cleanup = now_ts
        except Exception as e:
            log.exception("background loop lỗi: %s", e)
        time.sleep(REFRESH_INTERVAL)

# ============================================================
# Serialize
# ============================================================
def _row_to_dict(row, include_payload=False):
    d = dict(row)
    if not include_payload:
        d.pop("last_payload", None)
    try:
        d["events"] = json.loads(d.get("last_events") or "[]")
    except Exception:
        d["events"] = []
    d.pop("last_events", None)
    d["carrier_name"] = track.CARRIERS.get(d["carrier"], {}).get("name", d["carrier"])
    return d

# ============================================================
# ROUTES — Web
# ============================================================
@app.route("/")
def index():
    token = (request.args.get("login") or "").strip()
    if token:
        db = get_db()
        row = db.execute("SELECT * FROM users WHERE login_token = ?", (token,)).fetchone()
        if row:
            session.permanent = True
            session["user_id"] = row["id"]
            return redirect(url_for("index"))
        return render_template("index.html",
            carriers=track.CARRIERS, needs_login=True,
            error="Mã đăng nhập không hợp lệ hoặc đã hết hạn.")

    user = current_user()
    if not user:
        return render_template("index.html",
            carriers=track.CARRIERS, needs_login=True, error=None)
    return render_template("index.html",
        carriers=track.CARRIERS, needs_login=False,
        user={"id": user["id"], "name": user["name"] or "bạn"})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})

# ============================================================
# ROUTES — API
# ============================================================
@app.route("/api/me")
@require_user
def api_me():
    u = g.user
    return jsonify({"id": u["id"], "name": u["name"], "telegram_id": u["telegram_id"]})

@app.route("/api/detect", methods=["POST"])
@require_user
def api_detect():
    code = (request.get_json(force=True).get("tracking_code") or "").strip()
    return jsonify({
        "carrier": track.detect_carrier(code),
        "manual":  track.manual_lookup_for(code),
    })

@app.route("/api/raw/<carrier>/<code>")
@require_user
def api_raw(carrier, code):
    if carrier not in track.CARRIERS:
        return jsonify({"ok": False, "error": f"Hãng không hỗ trợ: {carrier}"}), 400
    phone = (request.args.get("phone") or "").strip() or None
    try:
        data = track.call(carrier, code.upper(), phone)
    except Exception as e:
        return jsonify({
            "ok": False, "carrier": carrier, "code": code.upper(),
            "error": str(e),
            "manual": track.manual_lookup_for(code),
        }), 500
    if isinstance(data, dict) and data.get("need_phone") and not phone:
        return jsonify({
            "ok": False, "carrier": carrier, "code": code.upper(),
            "error": "Cần 4 số cuối SĐT", "need_phone": True,
        }), 400
    return jsonify({
        "ok": True,
        "carrier": carrier,
        "code": code.upper(),
        "carrier_name": track.CARRIERS[carrier]["name"],
        "extracted": track.extract_all(data),
        "raw": data,
    })

@app.route("/api/trackings", methods=["GET"])
@require_user
def list_trackings():
    # KHÔNG gọi cleanup_old() ở đây nữa (GET = read only)
    db = get_db()
    rows = db.execute(
        "SELECT * FROM trackings WHERE user_id = ? ORDER BY id DESC",
        (g.user["id"],),
    ).fetchall()
    return jsonify([_row_to_dict(r) for r in rows])

@app.route("/api/trackings", methods=["POST"])
@require_user
def add_tracking():
    body = request.get_json(force=True) or {}
    code = (body.get("tracking_code") or "").strip().upper()
    carrier = (body.get("carrier") or "").strip().lower()
    note = (body.get("note") or "").strip()[:200] or None
    phone = (body.get("phone") or "").strip()[:20] or None

    if not code:
        return jsonify({"error": "Thiếu mã vận đơn"}), 400
    if not carrier:
        carrier = track.detect_carrier(code)
        if not carrier:
            ml = track.manual_lookup_for(code)
            hint = f" Thử tra web: {ml['url']}" if ml and ml.get("url") else ""
            return jsonify({"error": "Không nhận diện được hãng." + hint}), 400
    if carrier not in track.CARRIERS:
        return jsonify({"error": f"Hãng không hỗ trợ: {carrier}"}), 400

    now = datetime.now(timezone.utc).isoformat()
    try:
        with _write_lock:
            db = get_db()
            cur = db.execute(
                """INSERT INTO trackings
                   (user_id, carrier, tracking_code, note, phone, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (g.user["id"], carrier, code, note, phone, now),
            )
            db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Mã này đã được theo dõi"}), 409

    db = get_db()
    row = db.execute("SELECT * FROM trackings WHERE id = ?", (cur.lastrowid,)).fetchone()
    fetch_result = refresh_one(row)
    row = db.execute("SELECT * FROM trackings WHERE id = ?", (row["id"],)).fetchone()
    return jsonify({"tracking": _row_to_dict(row), "fetch": fetch_result}), 201

@app.route("/api/trackings/<int:tid>", methods=["DELETE"])
@require_user
def delete_tracking(tid):
    with _write_lock:
        db = get_db()
        cur = db.execute("DELETE FROM trackings WHERE id = ? AND user_id = ?",
                         (tid, g.user["id"]))
        db.commit()
    if not cur.rowcount:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})

@app.route("/api/trackings/<int:tid>", methods=["PATCH"])
@require_user
def patch_tracking(tid):
    body = request.get_json(force=True) or {}
    fields = {}
    if "note" in body:
        fields["note"] = (body.get("note") or "").strip()[:200] or None
    if "phone" in body:
        fields["phone"] = (body.get("phone") or "").strip()[:20] or None
    if not fields:
        return jsonify({"error": "Không có trường nào để cập nhật"}), 400

    sets = ", ".join(f"{k} = ?" for k in fields)
    with _write_lock:
        db = get_db()
        cur = db.execute(f"UPDATE trackings SET {sets} WHERE id = ? AND user_id = ?",
                         (*fields.values(), tid, g.user["id"]))
        db.commit()
    if not cur.rowcount:
        return jsonify({"error": "not found"}), 404
    row = db.execute("SELECT * FROM trackings WHERE id = ?", (tid,)).fetchone()
    return jsonify({"tracking": _row_to_dict(row)})

@app.route("/api/trackings/<int:tid>/refresh", methods=["POST"])
@require_user
def refresh_tracking(tid):
    db = get_db()
    row = db.execute("SELECT * FROM trackings WHERE id = ? AND user_id = ?",
                     (tid, g.user["id"])).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    result = refresh_one(row)
    row = db.execute("SELECT * FROM trackings WHERE id = ?", (tid,)).fetchone()
    return jsonify({"tracking": _row_to_dict(row), "fetch": result})

@app.route("/api/trackings/<int:tid>/payload")
@require_user
def tracking_payload(tid):
    db = get_db()
    row = db.execute(
        "SELECT last_payload, last_events FROM trackings WHERE id = ? AND user_id = ?",
        (tid, g.user["id"]),
    ).fetchone()
    if not row or not row["last_payload"]:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "raw":    json.loads(row["last_payload"]),
        "events": json.loads(row["last_events"] or "[]"),
    })

# ============================================================
# TELEGRAM BOT
# ============================================================
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def tg_send(chat_id, text, parse_mode="HTML", disable_preview=True):
    try:
        requests.post(f"{TG_API}/sendMessage", json={
            "chat_id": chat_id, "text": text[:4000],
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }, timeout=20)
    except Exception as e:
        log.warning("tg_send lỗi: %s", e)

def _fmt_tracking_short(row):
    status = row["last_status"] or "Chưa có trạng thái"
    name = track.CARRIERS.get(row["carrier"], {}).get("name", row["carrier"])
    note = f"\n   📝 {row['note']}" if row["note"] else ""
    dlv = " ✅" if row["delivered_at"] else ""
    return f"• <code>{row['tracking_code']}</code> — {name}{dlv}\n   {status}{note}"

def _fmt_tracking_full(row):
    name = track.CARRIERS.get(row["carrier"], {}).get("name", row["carrier"])
    lines = [f"📦 <b>{row['tracking_code']}</b>", f"🚚 {name}"]
    if row["note"]:
        lines.append(f"📝 {row['note']}")
    if row["product_name"]:
        lines.append(f"🛍 {row['product_name']}")
    lines.append(f"Trạng thái: {row['last_status'] or 'Chưa có'}")
    if row["last_location"]:
        lines.append(f"📍 {row['last_location']}")
    if row["last_event_time"]:
        lines.append(f"🕒 {row['last_event_time']}")
    try:
        events = json.loads(row["last_events"] or "[]")
    except Exception:
        events = []
    if events:
        lines.append("\n<b>Hành trình:</b>")
        for ev in reversed(events[-8:]):
            lines.append(f"• {ev.get('time') or ''} — {ev.get('status') or ''}")
    return "\n".join(lines)

def _add_user_tracking(telegram_id, name, code, note=None, phone=None):
    user = get_or_create_user(telegram_id, name)
    carrier = track.detect_carrier(code)
    if not carrier:
        return None, "Không nhận diện được hãng."
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _write_lock, closing(_db_connect()) as db:
            cur = db.execute(
                """INSERT INTO trackings (user_id, carrier, tracking_code, note, phone, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (user["id"], carrier, code, note, phone, now),
            )
            db.commit()
            row = dict(db.execute("SELECT * FROM trackings WHERE id = ?", (cur.lastrowid,)).fetchone())
    except sqlite3.IntegrityError:
        return None, "Mã này đã được theo dõi rồi."
    with app.app_context():
        refresh_one(row)
    with closing(_db_connect()) as db:
        row = db.execute("SELECT * FROM trackings WHERE id = ?", (row["id"],)).fetchone()
    return row, None

def handle_update(update: dict):
    msg = update.get("message") or update.get("edited_message") or {}
    if not msg:
        return
    chat_id = msg.get("chat", {}).get("id")
    from_user = msg.get("from", {}) or {}
    tg_id = from_user.get("id")
    text = (msg.get("text") or "").strip()
    if not tg_id or not text.startswith("/"):
        return

    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    display_name = ((from_user.get("first_name") or "") + " " +
                    (from_user.get("last_name") or "")).strip() \
                   or from_user.get("username") or str(tg_id)

    if cmd == "/start":
        user = get_or_create_user(tg_id, display_name)
        link = f"{WEB_BASE_URL}/?login={user['login_token']}"
        tg_send(chat_id,
            f"👋 Chào <b>{display_name}</b>!\n\n"
            f"🔗 <b>Link đăng nhập web của bạn:</b>\n{link}\n\n"
            f"Lưu lại link này.\n\n"
            f"<b>Lệnh:</b> /login /list /add /check /del /refresh /help")

    elif cmd == "/login":
        user = get_or_create_user(tg_id, display_name)
        link = f"{WEB_BASE_URL}/?login={user['login_token']}"
        tg_send(chat_id, f"🔗 <b>Link đăng nhập của bạn:</b>\n{link}")

    elif cmd == "/help":
        tg_send(chat_id,
            "<b>Các lệnh:</b>\n"
            "/start — bắt đầu\n/login — lấy link đăng nhập web\n"
            "/list — xem danh sách vận đơn\n"
            "/add CODE [SĐT] — thêm vận đơn\n"
            "/check CODE [SĐT] — tra cứu không lưu\n"
            "/del CODE — xóa\n/refresh CODE — cập nhật 1 mã")

    elif cmd == "/list":
        with closing(_db_connect()) as db:
            user = db.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,)).fetchone()
            if not user:
                tg_send(chat_id, "📭 Bạn chưa có vận đơn nào. Dùng /add CODE để thêm.")
                return
            rows = db.execute(
                "SELECT * FROM trackings WHERE user_id = ? ORDER BY id DESC",
                (user["id"],),
            ).fetchall()
        if not rows:
            tg_send(chat_id, "📭 Bạn chưa có vận đơn nào. Dùng /add CODE để thêm.")
            return
        body = "\n".join(_fmt_tracking_short(r) for r in rows[:30])
        extra = f"\n\n<i>(còn {len(rows)-30} mã nữa)</i>" if len(rows) > 30 else ""
        tg_send(chat_id, f"📦 <b>Vận đơn của bạn ({len(rows)}):</b>\n\n{body}{extra}")

    elif cmd in ("/add", "/them"):
        if not args:
            tg_send(chat_id, "Cú pháp: <code>/add CODE [SĐT]</code>")
            return
        code = args[0].strip().upper()
        phone = args[1].strip() if len(args) > 1 else None
        note = " ".join(args[2:]).strip()[:200] or None if len(args) > 2 else None
        row, err = _add_user_tracking(tg_id, display_name, code, note, phone)
        if err:
            tg_send(chat_id, f"❌ {err}")
            return
        tg_send(chat_id, f"✅ Đã thêm!\n\n{_fmt_tracking_full(row)}")

    elif cmd in ("/check", "/tra"):
        if not args:
            tg_send(chat_id, "Cú pháp: <code>/check CODE [SĐT]</code>")
            return
        code = args[0].strip().upper()
        phone = args[1].strip() if len(args) > 1 else None
        carrier = track.detect_carrier(code)
        if not carrier:
            ml = track.manual_lookup_for(code)
            if ml and ml.get("url"):
                tg_send(chat_id, f"ℹ️ {ml['name']} cần tra qua web:\n{ml['url']}")
            else:
                tg_send(chat_id, "❌ Không nhận diện được hãng của mã này.")
            return
        try:
            data = track.call(carrier, code, phone)
        except Exception as e:
            tg_send(chat_id, f"❌ Lỗi gọi API: {e}")
            return
        if data.get("need_phone") and not phone:
            tg_send(chat_id, "📞 Cần 4 số cuối SĐT: /check CODE 1234")
            return
        info = track.extract_all(data)
        lines = [f"📦 <b>{code}</b> ({track.CARRIERS[carrier]['name']})",
                 f"Trạng thái: {info.get('status') or 'Chưa có'}"]
        if info.get("location"):
            lines.append(f"📍 {info['location']}")
        if info.get("event_time"):
            lines.append(f"🕒 {info['event_time']}")
        if info.get("events"):
            lines.append("\n<b>Hành trình:</b>")
            for ev in reversed(info["events"][-8:]):
                lines.append(f"• {ev.get('time') or ''} — {ev.get('status') or ''}")
        tg_send(chat_id, "\n".join(lines))

    elif cmd in ("/del", "/xoa"):
        if not args:
            tg_send(chat_id, "Cú pháp: <code>/del CODE</code>")
            return
        code = args[0].strip().upper()
        user = get_or_create_user(tg_id, display_name)
        with _write_lock, closing(_db_connect()) as db:
            cur = db.execute(
                "DELETE FROM trackings WHERE user_id = ? AND tracking_code = ?",
                (user["id"], code))
            db.commit()
        tg_send(chat_id, "✅ Đã xóa." if cur.rowcount else "❌ Không tìm thấy mã này.")

    elif cmd in ("/refresh", "/capnhat"):
        if not args:
            tg_send(chat_id, "Cú pháp: <code>/refresh CODE</code>")
            return
        code = args[0].strip().upper()
        user = get_or_create_user(tg_id, display_name)
        with app.app_context():
            db = get_db()
            row = db.execute(
                "SELECT * FROM trackings WHERE user_id = ? AND tracking_code = ?",
                (user["id"], code)).fetchone()
            if not row:
                tg_send(chat_id, "❌ Không tìm thấy mã này.")
                return
            result = refresh_one(row)
            row = db.execute("SELECT * FROM trackings WHERE id = ?", (row["id"],)).fetchone()
            if result.get("error"):
                tg_send(chat_id, f"⚠️ Cập nhật lỗi: {result['error']}")
            else:
                tg_send(chat_id, f"🔄 Đã cập nhật\n\n{_fmt_tracking_full(row)}")
    else:
        tg_send(chat_id, "❓ Lệnh không hợp lệ. Dùng /help.")

def bot_loop():
    if not TELEGRAM_BOT_TOKEN:
        log.info("TELEGRAM_BOT_TOKEN không có — bot không chạy.")
        return
    log.info("Telegram bot polling bắt đầu.")
    offset = 0
    while True:
        try:
            r = requests.get(f"{TG_API}/getUpdates",
                             params={"offset": offset + 1, "timeout": 25},
                             timeout=35)
            data = r.json()
            if not data.get("ok"):
                log.warning("getUpdates lỗi: %s", data)
                time.sleep(5)
                continue
            for u in data.get("result", []):
                offset = max(offset, u["update_id"])
                try:
                    handle_update(u)
                except Exception as e:
                    log.exception("handle_update lỗi: %s", e)
        except Exception as e:
            log.warning("bot_loop lỗi: %s", e)
            time.sleep(5)

# ============================================================
# Bootstrap
# ============================================================
init_db()

# Chạy cleanup 1 lần khi khởi động
try:
    cleanup_old()
except Exception as e:
    log.warning("cleanup khởi động lỗi: %s", e)

if os.getenv("DISABLE_BACKGROUND", "0") != "1":
    threading.Thread(target=background_loop, daemon=True).start()

if TELEGRAM_BOT_TOKEN:
    threading.Thread(target=bot_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
