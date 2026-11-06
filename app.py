import os
import re
import json
import time
import secrets
import hashlib
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from threading import Thread
from functools import wraps

from flask import Flask, render_template, request, jsonify, g

import track
import bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tracker")

app = Flask(__name__)

# ============================================================
# CẤU HÌNH
# ============================================================
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL_SECONDS", "600"))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600"))
DELETE_AFTER_DELIVERED_DAYS = int(os.getenv("DELETE_AFTER_DELIVERED_DAYS", "7"))
LOGIN_CODE_TTL_MIN = int(os.getenv("LOGIN_CODE_TTL_MINUTES", "5"))
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "")

DB_DIR = os.getenv("DB_DIR", "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "tracker.db")

CARRIERS = {
    "spx": {"name": "Shopee Express"},
    "ghn": {"name": "Giao Hàng Nhanh"},
    "giaohangtietkiem": {"name": "Giao Hàng Tiết Kiệm"},
    "jtexpress": {"name": "J&T Express"},
    "viettelpost": {"name": "Viettel Post"},
    "ninjavan": {"name": "Ninja Van"},
    "lex": {"name": "Lazada Express"},
    "tiktok-express": {"name": "TikTok Express"},
    "vnpost": {"name": "Vietnam Post"},
    "ahamove": {"name": "Ahamove"},
}
PHONE_REQUIRED = {c for c, _, needs in track.CARRIERS if needs}

DELIVERED_KEYWORDS = [
    "giao hàng thành công", "giao thành công", "đã giao hàng", "đã giao",
    "phát thành công", "đã phát", "giao hang thanh cong", "da giao",
    "phat thanh cong", "hoàn tất", "hoan tat", "đã nhận hàng", "đã nhận",
    "delivered", "delivery success", "delivery successful",
    "completed", "signed", "received",
]

# ============================================================
# DB
# ============================================================
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def _db():
    """Connection riêng cho thread / background."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

TRACKINGS_DDL = """
CREATE TABLE trackings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 0,
    carrier TEXT NOT NULL,
    tracking_code TEXT NOT NULL,
    note TEXT,
    phone TEXT,
    product_name TEXT,
    last_status TEXT,
    last_status_code TEXT,
    last_phase TEXT,
    last_location TEXT,
    last_next_loc TEXT,
    last_event_time TEXT,
    last_events TEXT,
    last_payload TEXT,
    last_checked_at TEXT,
    delivered_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, carrier, tracking_code)
);
CREATE INDEX IF NOT EXISTS idx_trackings_user ON trackings(user_id);
CREATE INDEX IF NOT EXISTS idx_trackings_active ON trackings(is_active);
CREATE INDEX IF NOT EXISTS idx_trackings_delivered ON trackings(delivered_at);
"""

def _hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                notifications_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_seen_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_codes (
                code TEXT PRIMARY KEY,
                user_id INTEGER,
                purpose TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        """)

        # Trackings: migrate nếu cần
        existing = {r[1] for r in db.execute("PRAGMA table_info(trackings)").fetchall()}
        if not existing:
            db.executescript(TRACKINGS_DDL)
        elif "user_id" not in existing or "phone" not in existing:
            log.info("Migrating trackings table…")
            db.execute("ALTER TABLE trackings RENAME TO trackings_old")
            db.executescript(TRACKINGS_DDL)
            old_cols = {r[1] for r in db.execute("PRAGMA table_info(trackings_old)").fetchall()}
            new_cols = {r[1] for r in db.execute("PRAGMA table_info(trackings)").fetchall()}
            common = old_cols & new_cols
            cols = ", ".join(common)
            db.execute(f"INSERT INTO trackings ({cols}) SELECT {cols} FROM trackings_old")
            db.execute("DROP TABLE trackings_old")
        else:
            db.executescript(TRACKINGS_DDL)

        db.commit()

    # Bootstrap admin user
    if ADMIN_PASSWORD and ADMIN_TELEGRAM_ID.isdigit():
        _ensure_admin_user()

def _ensure_admin_user():
    tid = int(ADMIN_TELEGRAM_ID)
    now = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        row = db.execute("SELECT id FROM users WHERE id=?", (tid,)).fetchone()
        if row:
            db.execute("UPDATE users SET is_admin=1 WHERE id=?", (tid,))
        else:
            db.execute(
                "INSERT INTO users (id, first_name, is_admin, created_at) VALUES (?,?,1,?)",
                (tid, ADMIN_USERNAME, now),
            )
        db.commit()

# ============================================================
# AUTH
# ============================================================
def _gen_code():
    return f"{secrets.randbelow(1000000):06d}"

def _create_login_code(user_id=None, purpose="user"):
    code = _gen_code()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=LOGIN_CODE_TTL_MIN)
    with _db() as db:
        db.execute(
            "INSERT INTO login_codes (code, user_id, purpose, created_at, expires_at) "
            "VALUES (?,?,?,?,?)",
            (code, user_id, purpose, now.isoformat(), expires.isoformat()),
        )
        db.commit()
    return code

def _consume_login_code(code, purpose):
    now = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        row = db.execute(
            "SELECT * FROM login_codes WHERE code=? AND purpose=? AND used=0 AND expires_at>?",
            (code, purpose, now),
        ).fetchone()
        if not row:
            return None
        db.execute("UPDATE login_codes SET used=1 WHERE code=?", (code,))
        db.commit()
        return dict(row)

def _create_session(user_id, is_admin):
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SESSION_TTL_DAYS)
    with _db() as db:
        db.execute(
            "INSERT INTO sessions (token, user_id, is_admin, created_at, expires_at) "
            "VALUES (?,?,?,?,?)",
            (token, user_id, int(is_admin), now.isoformat(), expires.isoformat()),
        )
        db.commit()
    return token

def _session_from_request():
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    if not token:
        token = request.cookies.get("session_token")
    if not token:
        return None
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    row = db.execute(
        "SELECT * FROM sessions WHERE token=? AND expires_at>?", (token, now)
    ).fetchone()
    if not row:
        return None
    user = db.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    return {"session": dict(row), "user": dict(user) if user else None}

def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        ctx = _session_from_request()
        if not ctx:
            return jsonify({"error": "Chưa đăng nhập"}), 401
        g.auth = ctx
        return fn(*args, **kwargs)
    return wrapper

def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        ctx = _session_from_request()
        if not ctx:
            return jsonify({"error": "Chưa đăng nhập"}), 401
        if not ctx["session"]["is_admin"]:
            return jsonify({"error": "Cần quyền admin"}), 403
        g.auth = ctx
        return fn(*args, **kwargs)
    return wrapper

def current_user_id():
    ctx = getattr(g, "auth", None)
    return ctx["session"]["user_id"] if ctx else None

def is_admin_session():
    ctx = getattr(g, "auth", None)
    return bool(ctx and ctx["session"]["is_admin"])

# ============================================================
# API track — bọc track.py
# ============================================================
def _call_api(carrier, code, phone=None):
    try:
        res = track.call(carrier, code, phone)
        if not isinstance(res, dict):
            return {"__error__": f"Invalid type {type(res).__name__}"}
        return res
    except Exception as e:
        log.warning("call %s/%s: %s", carrier, code, e)
        return {"__error__": str(e)}

# ============================================================
# Key pools / extraction (giữ nguyên như bản trước)
# ============================================================
STATUS_KEYS = (
    "status_text", "statusText", "status_name", "statusName",
    "status_description", "statusDescription", "status_desc",
    "last_status", "order_status", "orderStatus", "orderState",
    "current_status", "currentStatus", "current_state", "currentState",
    "shipping_status", "delivery_status", "deliveryStatus",
    "state_text", "stateText", "state",
    "description", "desc", "message", "msg",
    "status",
)
STATUS_CODE_KEYS = (
    "status_code", "statusCode", "reason_code", "reasonCode",
    "state_code", "stateCode", "phase_code", "phaseCode",
    "event_code", "eventCode", "code",
)
PHASE_KEYS = (
    "phase", "stage", "phase_name", "phaseName", "stage_name", "stageName",
    "milestone", "group", "status_group", "statusGroup", "type",
)
LOCATION_KEYS = (
    "location", "current_location", "currentLocation",
    "from_location", "fromLocation",
    "address", "hub", "station", "warehouse",
    "current_hub", "currentHub", "current_warehouse",
    "pickup_warehouse", "office_name", "officeName",
)
NEXT_LOCATION_KEYS = (
    "next_location", "nextLocation", "next_hub", "nextHub",
    "next_station", "nextStation", "next_warehouse", "nextWarehouse",
    "destination", "to", "to_location", "toLocation",
    "target_hub", "targetHub",
)
PRODUCT_KEYS = (
    "product_name", "productName", "item_name", "itemName",
    "goods_name", "goodsName", "name_product", "product_title", "productTitle",
    "product", "item", "goods", "title", "name",
)
TIME_KEYS = (
    "event_time", "eventTime", "occurred_at", "occurredAt",
    "updated_at", "updatedAt", "update_time", "updateTime",
    "created_at", "createdAt", "create_time", "createTime",
    "time", "timestamp", "date", "datetime", "ts",
    "action_time", "actionTime", "action_at", "actionAt",
    "insert_time", "insertTime", "insert_at", "insertAt",
    "record_time", "recordTime", "reported_at", "reportedAt",
    "tracking_time", "trackingTime", "log_time", "logTime",
    "occur_time", "occurTime", "report_time", "reportTime",
    "actual_time", "actualTime",
    "time_text", "timeText", "format_time", "formatTime",
    "update_date", "updateDate", "create_date", "createDate",
    "event_date", "eventDate",
)
EVENT_LIST_KEYS = (
    "events", "checkpoints", "history", "logs", "tracking_logs",
    "trackings", "routes", "journeys", "steps", "timeline",
    "history_log", "tracking_history", "tracking",
    "tracks", "track_list", "trackList",
    "progress", "progress_list", "progressList",
    "nodes", "points", "milestones",
    "activity", "activities", "details",
    "records",
)
EVENT_DESC_KEYS = (
    "description", "desc", "message", "msg", "note", "detail", "content",
    "buyer_description",
)

_ENVELOPE_VALUES = {"success","ok","error","true","false","fail","failed",
                    "1","0","null","none","yes","no"}
_ENVELOPE_CODES = {"R00","00","000","200","201","OK","SUCCESS","ERROR","FAIL","FAILED"}

def _is_envelope_value(v):
    if v is None: return True
    return str(v).strip().lower() in _ENVELOPE_VALUES

def _is_envelope_code(v):
    if v is None: return True
    return str(v).strip().upper() in _ENVELOPE_CODES

def _unwrap(data):
    if isinstance(data, dict):
        for k in ("data", "result", "response", "payload", "tracking_data"):
            v = data.get(k)
            if isinstance(v, (dict, list)) and v:
                return v
    return data

def _walk_values(obj, keys):
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, "", []):
                return obj[k]
        for v in obj.values():
            found = _walk_values(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _walk_values(item, keys)
            if found is not None:
                return found
    return None

def _find_list_by_keys(obj, keys):
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in obj.values():
            found = _find_list_by_keys(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_list_by_keys(item, keys)
            if found:
                return found
    return None

def _find_events_anywhere(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                first = v[0]
                if (any(kk in first for kk in STATUS_KEYS)
                    or any(kk in first for kk in TIME_KEYS)
                    or any(kk in first for kk in EVENT_DESC_KEYS)):
                    return v
        for v in obj.values():
            found = _find_events_anywhere(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_events_anywhere(item)
            if found:
                return found
    return None

def _extract_string(v):
    if v is None: return None
    if isinstance(v, str):
        s = v.strip(); return s or None
    if isinstance(v, (int, float)): return str(v)
    if isinstance(v, dict):
        for k in ("name", "text", "label", "title", "description",
                  "desc", "content", "value", "status"):
            if k in v:
                inner = _extract_string(v[k])
                if inner:
                    return inner
    return None

def _normalize_time(v):
    if v is None: return None
    if isinstance(v, (int, float)):
        n = float(v)
        if n > 1e12: n /= 1000
        if 1e9 < n < 2_100_000_000:
            try: return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
            except Exception: pass
        return str(v)
    if isinstance(v, str):
        s = v.strip()
        if not s: return None
        if s.isdigit():
            try:
                n = float(s)
                if n > 1e12: n /= 1000
                if 1e9 < n < 2_100_000_000:
                    return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
            except Exception: pass
        return s
    if isinstance(v, dict):
        for k in TIME_KEYS:
            if k in v:
                inner = _normalize_time(v[k])
                if inner: return inner
    return None

def _parse_time_for_sort(t):
    if not t: return None
    try: return datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except Exception: return None

def _normalize_event_order(events):
    if len(events) < 2: return events
    times = [_parse_time_for_sort(ev.get("time")) for ev in events]
    if all(t is not None for t in times):
        return events if times[0] <= times[-1] else list(reversed(events))
    if times[0] is not None and times[-1] is not None:
        return events if times[0] <= times[-1] else list(reversed(events))
    return list(reversed(events))

def _extract_event_field(ev, keys, is_code=False, is_time=False):
    if isinstance(ev, dict):
        for k in keys:
            if k in ev and ev[k] not in (None, "", []):
                val = ev[k]
                if is_time:
                    t = _normalize_time(val)
                    if t: return t
                    continue
                s = _extract_string(val)
                if not s: continue
                if is_code and _is_envelope_code(s): continue
                return s
    v = _walk_values(ev, keys)
    if is_time:
        return _normalize_time(v)
    s = _extract_string(v)
    if is_code and s and _is_envelope_code(s):
        return None
    return s

def _events_of_dicts(res):
    """Trả events (cũ → mới) từ format track.py."""
    data = res.get("data") or {}
    raw = (
        (data.get("sls_tracking_info") or {}).get("records")
        or (res.get("sls_tracking_info") or {}).get("records")
        or data.get("events") or res.get("events") or []
    )
    if not isinstance(raw, list):
        return []
    out = []
    for e in raw:
        if not isinstance(e, dict): continue
        ts = e.get("actual_time")
        if ts:
            try:
                when = datetime.fromtimestamp(ts).strftime("%H:%M %d/%m/%Y")
                sort_ts = float(ts)
            except Exception:
                when, sort_ts = str(ts), 0.0
        else:
            when = ("%s %s" % (e.get("time", ""), e.get("date", ""))).strip()
            sort_ts = 0.0

        loc = e.get("current_location")
        if isinstance(loc, dict):
            addr = loc.get("full_address") or ""
        elif isinstance(loc, str):
            addr = loc
        else:
            addr = ""
        addr = addr or e.get("address") or ""

        next_loc = (
            e.get("next_location") or e.get("next_station")
            or (e.get("next_location_info") or {}).get("full_address") or ""
        )

        out.append({
            "status": str(e.get("status") or e.get("status_name") or ""),
            "status_code": str(e.get("status_code") or e.get("code") or ""),
            "phase": str(e.get("phase") or e.get("stage") or ""),
            "location": str(addr or ""),
            "next_location": str(next_loc or ""),
            "time": when or "",
            "description": str(
                e.get("buyer_description") or e.get("description")
                or e.get("status") or ""
            ),
            "_ts": sort_ts,
        })
    if out:
        if all(ev["_ts"] > 0 for ev in out):
            out.sort(key=lambda x: x["_ts"])
        else:
            out.reverse()
    for ev in out:
        ev.pop("_ts", None)
    return out

def _extract_events_generic(data):
    events = _find_list_by_keys(data, EVENT_LIST_KEYS) or _find_events_anywhere(data)
    if not events: return []
    out = []
    for ev in events:
        if not isinstance(ev, dict): continue
        row = {
            "status": _extract_event_field(ev, STATUS_KEYS) or "",
            "status_code": _extract_event_field(ev, STATUS_CODE_KEYS, is_code=True) or "",
            "phase": _extract_event_field(ev, PHASE_KEYS) or "",
            "location": _extract_event_field(ev, LOCATION_KEYS) or "",
            "next_location": _extract_event_field(ev, NEXT_LOCATION_KEYS) or "",
            "time": _extract_event_field(ev, TIME_KEYS, is_time=True) or "",
            "description": _extract_event_field(ev, EVENT_DESC_KEYS) or "",
        }
        if any(row.values()):
            out.append(row)
    return _normalize_event_order(out)

def _pick_status_generic(root):
    for group in (
        ("status_text", "statusText", "status_name", "statusName",
         "status_description", "statusDescription", "status_desc"),
        ("current_status", "currentStatus", "current_state", "currentState",
         "shipping_status", "delivery_status", "deliveryStatus"),
        ("order_status", "orderStatus", "last_status"),
        ("state_text", "stateText", "state"),
        ("description", "desc", "message", "msg"),
        ("status",),
    ):
        v = _walk_values(root, group)
        s = _extract_string(v)
        if s and not _is_envelope_value(s):
            return s
    return None

def _is_delivered(s):
    if not s: return False
    s = s.lower()
    return any(k in s for k in DELIVERED_KEYWORDS)

def _extract_all(res):
    events = _events_of_dicts(res) or _extract_events_generic(res)
    last = events[-1] if events else {}
    order = res.get("order") or (res.get("data") or {}).get("order_info") or {}
    order_status = order.get("status_name") or order.get("tracking_code_group_name")
    status = order_status or last.get("status") or last.get("description")
    if not status:
        status = _pick_status_generic(_unwrap(res))
    product = _extract_string(_walk_values(res, PRODUCT_KEYS))
    delivered = (
        _is_delivered(status)
        or _is_delivered(last.get("status"))
        or _is_delivered(last.get("description"))
    )
    return {
        "status": status,
        "status_code": last.get("status_code") or "",
        "phase": last.get("phase") or "",
        "location": last.get("location") or "",
        "next_location": last.get("next_location") or "",
        "event_time": last.get("time") or "",
        "product": product,
        "events": events,
        "delivered": delivered,
    }

# ============================================================
# Refresh + notify
# ============================================================
def _do_refresh(row, notify=False):
    if not row["is_active"]:
        return {"skipped": True, "status": row["last_status"]}

    carrier = row["carrier"]
    code = row["tracking_code"]
    phone = row["phone"] if "phone" in row.keys() else None
    old_status = row["last_status"]
    user_id = row["user_id"] if "user_id" in row.keys() else 0

    res = _call_api(carrier, code, phone)
    if res.get("__error__"):
        return {"error": res["__error__"]}

    now = datetime.now(timezone.utc).isoformat()

    if res.get("need_phone"):
        msg = "Cần 4 số cuối SĐT" if not phone else "SĐT không đúng"
        with _db() as db:
            db.execute("UPDATE trackings SET last_status=?, last_checked_at=? WHERE id=?",
                       (msg, now, row["id"]))
            db.commit()
        return {"error": msg, "need_phone": not bool(phone)}

    info = _extract_all(res)
    if not info["events"] and not info["status"]:
        msg = res.get("message") or res.get("msg") or "Không có dữ liệu"
        with _db() as db:
            db.execute("UPDATE trackings SET last_status=?, last_checked_at=? WHERE id=?",
                       (msg, now, row["id"]))
            db.commit()
        return {"error": msg}

    new_status = info["status"] or "unknown"

    with _db() as db:
        db.execute("""
            UPDATE trackings
               SET last_status=?, last_status_code=COALESCE(?, last_status_code),
                   last_phase=COALESCE(?, last_phase),
                   last_location=COALESCE(?, last_location),
                   last_next_loc=COALESCE(?, last_next_loc),
                   last_event_time=COALESCE(?, last_event_time),
                   product_name=COALESCE(?, product_name),
                   last_events=?, last_payload=?, last_checked_at=?,
                   delivered_at=CASE WHEN ? THEN COALESCE(delivered_at, ?) ELSE delivered_at END,
                   is_active=CASE WHEN ? THEN 0 ELSE is_active END
             WHERE id=?
        """, (
            new_status, info["status_code"], info["phase"], info["location"],
            info["next_location"], info["event_time"], info["product"],
            json.dumps(info["events"], ensure_ascii=False),
            json.dumps(res, ensure_ascii=False)[:200_000],
            now, info["delivered"], now, info["delivered"], row["id"],
        ))
        db.commit()

    # Notify on status change
    if (notify and user_id and old_status and old_status != new_status):
        with _db() as db:
            u = db.execute(
                "SELECT notifications_enabled FROM users WHERE id=?", (user_id,)
            ).fetchone()
        if u and u["notifications_enabled"]:
            icon = "✅" if info["delivered"] else "🔔"
            cname = CARRIERS.get(carrier, {}).get("name", carrier)
            lines = [
                f"{icon} <b>Cập nhật vận đơn</b>",
                f"📦 <code>{code}</code> — {cname}",
                f"🔄 Trạng thái: <b>{new_status}</b>",
            ]
            if info.get("location"):
                lines.append(f"📍 {info['location']}")
            bot.send_message(user_id, "\n".join(lines))

    return {
        "status": new_status,
        "status_code": info["status_code"],
        "product": info["product"],
        "events_count": len(info["events"]),
        "delivered": info["delivered"],
    }

# ============================================================
# Bot callbacks
# ============================================================
def _register_user(user):
    uid = user.get("id")
    if not uid: return
    now = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        db.execute("""
            INSERT INTO users (id, username, first_name, last_name, last_seen_at, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                last_seen_at=excluded.last_seen_at
        """, (uid, user.get("username"), user.get("first_name"),
              user.get("last_name"), now, now))
        # nếu là admin telegram id → set is_admin
        if ADMIN_TELEGRAM_ID.isdigit() and int(ADMIN_TELEGRAM_ID) == uid:
            db.execute("UPDATE users SET is_admin=1 WHERE id=?", (uid,))
        db.commit()

def _get_user_name(uid):
    with _db() as db:
        row = db.execute("SELECT first_name, username FROM users WHERE id=?", (uid,)).fetchone()
    return (row["first_name"] or row["username"] or f"user{uid}") if row else None

def _bot_user_info(uid):
    with _db() as db:
        row = db.execute("""
            SELECT u.*, (SELECT COUNT(*) FROM trackings t WHERE t.user_id=u.id) as tracking_count
            FROM users u WHERE u.id=?
        """, (uid,)).fetchone()
    return dict(row) if row else {}

def _bot_add_tracking(user_id, code, phone):
    carrier = detect_carrier(code)
    if not carrier:
        return {"error": "Không nhận diện được hãng"}
    if carrier in PHONE_REQUIRED and not phone:
        return {"error": "Cần SĐT", "need_phone": True}
    with _db() as db:
        try:
            cur = db.execute(
                "INSERT INTO trackings (user_id, carrier, tracking_code, phone, created_at) "
                "VALUES (?,?,?,?,?)",
                (user_id, carrier, code, phone, datetime.now(timezone.utc).isoformat()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            return {"error": "Đơn này đã có trong danh sách"}
        row = db.execute("SELECT * FROM trackings WHERE id=?", (cur.lastrowid,)).fetchone()
        _do_refresh(row, notify=False)
        row = db.execute("SELECT * FROM trackings WHERE id=?", (cur.lastrowid,)).fetchone()
    return {"tracking": _row_to_dict(row)}

def _bot_list_trackings(uid):
    with _db() as db:
        rows = db.execute(
            "SELECT * FROM trackings WHERE user_id=? ORDER BY id DESC", (uid,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]

def _bot_refresh_tracking(uid, code):
    with _db() as db:
        row = db.execute(
            "SELECT * FROM trackings WHERE user_id=? AND tracking_code=?",
            (uid, code),
        ).fetchone()
    if not row: return None
    info = _do_refresh(row, notify=False)
    with _db() as db:
        row = db.execute("SELECT * FROM trackings WHERE id=?", (row["id"],)).fetchone()
    return {"tracking": _row_to_dict(row), "fetch": info}

def _bot_detail_tracking(uid, code):
    with _db() as db:
        row = db.execute(
            "SELECT * FROM trackings WHERE user_id=? AND tracking_code=?",
            (uid, code),
        ).fetchone()
    if not row: return None
    d = _row_to_dict(row)
    return {"tracking": d, "events": d.get("events") or []}

def _bot_delete_tracking(uid, code):
    with _db() as db:
        cur = db.execute(
            "DELETE FROM trackings WHERE user_id=? AND tracking_code=?",
            (uid, code),
        )
        db.commit()
    return cur.rowcount > 0

def _bot_set_notifications(uid, enabled):
    with _db() as db:
        db.execute("UPDATE users SET notifications_enabled=? WHERE id=?",
                   (1 if enabled else 0, uid))
        db.commit()

def _bot_is_admin(uid):
    if ADMIN_TELEGRAM_ID.isdigit() and int(ADMIN_TELEGRAM_ID) == uid:
        return True
    with _db() as db:
        row = db.execute("SELECT is_admin FROM users WHERE id=?", (uid,)).fetchone()
    return bool(row and row["is_admin"])

def _bot_list_users():
    with _db() as db:
        rows = db.execute("""
            SELECT u.*, (SELECT COUNT(*) FROM trackings t WHERE t.user_id=u.id) as tracking_count
            FROM users u ORDER BY u.created_at DESC
        """).fetchall()
    return [dict(r) for r in rows]

def _bot_stats():
    with _db() as db:
        u = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        t = db.execute("SELECT COUNT(*) FROM trackings").fetchone()[0]
        a = db.execute("SELECT COUNT(*) FROM trackings WHERE is_active=1").fetchone()[0]
        d = db.execute("SELECT COUNT(*) FROM trackings WHERE delivered_at IS NOT NULL").fetchone()[0]
    return {"users": u, "total": t, "active": a, "delivered": d}

def _bot_broadcast(msg):
    with _db() as db:
        rows = db.execute("SELECT id FROM users WHERE notifications_enabled=1").fetchall()
    n = 0
    for r in rows:
        if bot.send_message(r["id"], msg):
            n += 1
        time.sleep(0.05)
    return n

bot.setup(
    register_user=_register_user,
    get_user_name=_get_user_name,
    bot_user_info=_bot_user_info,
    bot_add_tracking=_bot_add_tracking,
    bot_list_trackings=_bot_list_trackings,
    bot_refresh_tracking=_bot_refresh_tracking,
    bot_detail_tracking=_bot_detail_tracking,
    bot_delete_tracking=_bot_delete_tracking,
    bot_set_notifications=_bot_set_notifications,
    is_admin=_bot_is_admin,
    bot_list_users=_bot_list_users,
    bot_stats=_bot_stats,
    bot_broadcast=_bot_broadcast,
)

# ============================================================
# Cleanup / background
# ============================================================
def cleanup_old():
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=DELETE_AFTER_DELIVERED_DAYS)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        cur = db.execute(
            "DELETE FROM trackings WHERE delivered_at IS NOT NULL AND delivered_at < ?",
            (cutoff,),
        )
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso,))
        db.execute("DELETE FROM login_codes WHERE expires_at < ?", (now_iso,))
        db.commit()
    if cur.rowcount:
        log.info("cleanup: xóa %d đơn", cur.rowcount)

def background_loop():
    last_cleanup = 0.0
    while True:
        try:
            with _db() as db:
                rows = db.execute("SELECT * FROM trackings WHERE is_active=1").fetchall()
            log.info("background: refresh %d đơn", len(rows))
            for row in rows:
                try:
                    _do_refresh(row, notify=True)
                except Exception as e:
                    log.warning("refresh %s: %s", row["tracking_code"], e)
            ts = time.time()
            if ts - last_cleanup > CLEANUP_INTERVAL:
                cleanup_old()
                last_cleanup = ts
        except Exception as e:
            log.exception("background: %s", e)
        time.sleep(REFRESH_INTERVAL)

# ============================================================
# Auto-detect
# ============================================================
_RENDERED_MAP = {
    "vnPost": "vnpost",
    "ViettelPost": "viettelpost",
    "Ninja Van": "ninjavan",
    "GHTK": "giaohangtietkiem",
}
_SPX_RE = re.compile(r"^SPX")
_JT_RE = re.compile(r"^JT")
_LEX_RE = re.compile(r"^LEX")
_TTS_RE = re.compile(r"^TTS|^TK")
_VNP_RE = re.compile(r"^(EE|CP|RA|VN)\d")
_NJV_RE = re.compile(r"^NJVN|^SG")
_GHTK_RE = re.compile(r"^S\d{6,}")
_GHN_RE = re.compile(r"^\d{10,12}$")

def detect_carrier(code):
    c = (code or "").strip().upper()
    if not c: return None
    if track.NO_TRACKING[1].match(c): return "tiktok-express"
    for carrier, pat, _ in track.CARRIERS:
        if pat.match(c): return carrier
    for name, pat, _ in track.RENDERED:
        if pat.match(c):
            mapped = _RENDERED_MAP.get(name)
            if mapped: return mapped
    if _SPX_RE.search(c): return "spx"
    if _JT_RE.search(c): return "jtexpress"
    if _LEX_RE.search(c): return "lex"
    if _TTS_RE.search(c): return "tiktok-express"
    if _VNP_RE.search(c): return "vnpost"
    if _NJV_RE.search(c): return "ninjavan"
    if _GHTK_RE.search(c): return "giaohangtietkiem"
    if _GHN_RE.search(c): return "ghn"
    return None

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
    d["carrier_name"] = CARRIERS.get(d["carrier"], {}).get("name", d["carrier"])
    d["needs_phone"] = d["carrier"] in PHONE_REQUIRED
    return d

# ============================================================
# ROUTES
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/carriers")
def list_carriers():
    return jsonify({
        k: {"name": v["name"], "needs_phone": k in PHONE_REQUIRED}
        for k, v in CARRIERS.items()
    })

# ---------- AUTH ----------
@app.route("/api/auth/me")
def auth_me():
    ctx = _session_from_request()
    if not ctx:
        return jsonify({"authenticated": False})
    u = ctx["user"] or {}
    return jsonify({
        "authenticated": True,
        "user": {
            "id": u.get("id"),
            "username": u.get("username"),
            "first_name": u.get("first_name"),
            "last_name": u.get("last_name"),
            "is_admin": bool(ctx["session"]["is_admin"]),
            "notifications_enabled": bool(u.get("notifications_enabled", 1)),
        },
    })

@app.route("/api/auth/request-code", methods=["POST"])
def auth_request_code():
    body = request.get_json(force=True) or {}
    ident = (body.get("telegram") or "").strip()
    if not ident:
        return jsonify({"error": "Nhập Telegram ID hoặc @username"}), 400

    with _db() as db:
        if ident.startswith("@"):
            row = db.execute("SELECT * FROM users WHERE LOWER(username)=?",
                             (ident[1:].lower(),)).fetchone()
        elif ident.lstrip("-").isdigit():
            row = db.execute("SELECT * FROM users WHERE id=?", (int(ident),)).fetchone()
        else:
            row = None

    if not row:
        return jsonify({
            "error": "Không tìm thấy. Hãy mở bot, nhấn /start trước rồi thử lại.",
        }), 404

    code = _create_login_code(user_id=row["id"], purpose="user")
    sent = bot.send_message(
        row["id"],
        f"🔐 <b>Mã đăng nhập web</b>\n\n<code>{code}</code>\n\n"
        f"Hiệu lực {LOGIN_CODE_TTL_MIN} phút. Không chia sẻ cho ai.",
    )
    if not sent:
        return jsonify({"error": "Không gửi được mã. Kiểm tra bot cấu hình."}), 500

    return jsonify({"ok": True, "hint": "Đã gửi mã vào Telegram của bạn."})

@app.route("/api/auth/verify-code", methods=["POST"])
def auth_verify_code():
    body = request.get_json(force=True) or {}
    code = (body.get("code") or "").strip()
    if not code:
        return jsonify({"error": "Thiếu mã"}), 400

    row = _consume_login_code(code, "user")
    if not row:
        return jsonify({"error": "Mã sai hoặc đã hết hạn"}), 400

    token = _create_session(row["user_id"], is_admin=False)
    with _db() as db:
        db.execute("UPDATE users SET last_seen_at=? WHERE id=?",
                   (datetime.now(timezone.utc).isoformat(), row["user_id"]))
        db.commit()

    resp = jsonify({"ok": True, "token": token})
    resp.set_cookie("session_token", token, max_age=SESSION_TTL_DAYS*86400,
                    httponly=True, samesite="Lax")
    return resp

@app.route("/api/auth/admin/login", methods=["POST"])
def auth_admin_login():
    if not ADMIN_PASSWORD or not ADMIN_TELEGRAM_ID.isdigit():
        return jsonify({"error": "Admin chưa được cấu hình (ADMIN_PASSWORD/ADMIN_TELEGRAM_ID)"}), 400

    body = request.get_json(force=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if username != ADMIN_USERNAME or _hash_password(password) != _hash_password(ADMIN_PASSWORD):
        return jsonify({"error": "Sai tài khoản hoặc mật khẩu"}), 401

    _ensure_admin_user()
    code = _create_login_code(user_id=int(ADMIN_TELEGRAM_ID), purpose="admin")
    sent = bot.send_message(
        int(ADMIN_TELEGRAM_ID),
        f"🔐 <b>Mã đăng nhập admin</b>\n\n<code>{code}</code>\n\n"
        f"Hiệu lực {LOGIN_CODE_TTL_MIN} phút.",
    )
    if not sent:
        return jsonify({"error": "Không gửi được mã admin. Bot đã /start chưa?"}), 500
    return jsonify({"ok": True, "requires_code": True})

@app.route("/api/auth/admin/verify", methods=["POST"])
def auth_admin_verify():
    body = request.get_json(force=True) or {}
    code = (body.get("code") or "").strip()
    if not code:
        return jsonify({"error": "Thiếu mã"}), 400

    row = _consume_login_code(code, "admin")
    if not row:
        return jsonify({"error": "Mã sai hoặc đã hết hạn"}), 400

    token = _create_session(row["user_id"], is_admin=True)
    resp = jsonify({"ok": True, "token": token})
    resp.set_cookie("session_token", token, max_age=SESSION_TTL_DAYS*86400,
                    httponly=True, samesite="Lax")
    return resp

@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip() \
            or request.cookies.get("session_token")
    if token:
        with _db() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (token,))
            db.commit()
    resp = jsonify({"ok": True})
    resp.delete_cookie("session_token")
    return resp

# ---------- DETECT ----------
@app.route("/api/detect", methods=["POST"])
def api_detect():
    code = (request.get_json(force=True).get("tracking_code") or "").strip()
    return jsonify({"carrier": detect_carrier(code)})

# ---------- RAW ----------
@app.route("/api/raw/<carrier>/<code>")
@require_auth
def api_raw(carrier, code):
    if carrier not in CARRIERS:
        return jsonify({"error": f"Hãng không hỗ trợ: {carrier}"}), 400
    phone = (request.args.get("phone") or "").strip() or None
    res = _call_api(carrier, code.upper(), phone)
    if res.get("__error__"):
        return jsonify({"ok": False, "carrier": carrier, "code": code.upper(),
                        "error": res["__error__"]}), 500
    return jsonify({
        "ok": True,
        "carrier": carrier,
        "code": code.upper(),
        "carrier_name": CARRIERS[carrier]["name"],
        "need_phone": bool(res.get("need_phone")) and not phone,
        "extracted": _extract_all(res),
        "raw": res,
    })

# ---------- TRACKINGS ----------
def _get_owned_row(tid):
    db = get_db()
    row = db.execute("SELECT * FROM trackings WHERE id=?", (tid,)).fetchone()
    if not row:
        return None, ("not found", 404)
    if not is_admin_session() and row["user_id"] != current_user_id():
        return None, ("forbidden", 403)
    return row, None

@app.route("/api/trackings", methods=["GET"])
@require_auth
def list_trackings():
    cleanup_old()
    db = get_db()
    if is_admin_session():
        rows = db.execute("SELECT * FROM trackings ORDER BY id DESC").fetchall()
    else:
        rows = db.execute("SELECT * FROM trackings WHERE user_id=? ORDER BY id DESC",
                          (current_user_id(),)).fetchall()
    return jsonify([_row_to_dict(r) for r in rows])

@app.route("/api/trackings", methods=["POST"])
@require_auth
def add_tracking():
    body = request.get_json(force=True) or {}
    code = (body.get("tracking_code") or "").strip().upper()
    carrier = (body.get("carrier") or "").strip().lower()
    note = (body.get("note") or "").strip()[:200] or None
    phone = (body.get("phone") or "").strip()[:15] or None

    if not code:
        return jsonify({"error": "Thiếu mã vận đơn"}), 400
    if not carrier:
        carrier = detect_carrier(code)
        if not carrier:
            return jsonify({"error": "Không nhận diện được hãng"}), 400
    if carrier not in CARRIERS:
        return jsonify({"error": f"Hãng không hỗ trợ: {carrier}"}), 400
    if carrier in PHONE_REQUIRED and not phone:
        return jsonify({"error": f"Hãng này cần 4 số cuối SĐT.", "need_phone": True}), 400

    db = get_db()
    uid = current_user_id()
    now = datetime.now(timezone.utc).isoformat()
    try:
        cur = db.execute(
            "INSERT INTO trackings (user_id, carrier, tracking_code, note, phone, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (uid, carrier, code, note, phone, now),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Mã này đã có trong danh sách của bạn"}), 409

    row = db.execute("SELECT * FROM trackings WHERE id=?", (cur.lastrowid,)).fetchone()
    fetch_result = _do_refresh(row, notify=False)
    row = db.execute("SELECT * FROM trackings WHERE id=?", (row["id"],)).fetchone()
    return jsonify({"tracking": _row_to_dict(row), "fetch": fetch_result}), 201

@app.route("/api/trackings/<int:tid>", methods=["DELETE"])
@require_auth
def delete_tracking(tid):
    row, err = _get_owned_row(tid)
    if err: return jsonify({"error": err[0]}), err[1]
    db = get_db()
    db.execute("DELETE FROM trackings WHERE id=?", (tid,))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/trackings/<int:tid>", methods=["PATCH"])
@require_auth
def patch_tracking(tid):
    row, err = _get_owned_row(tid)
    if err: return jsonify({"error": err[0]}), err[1]

    body = request.get_json(force=True) or {}
    updates = {}
    if "note" in body:
        updates["note"] = (body.get("note") or "").strip()[:200] or None
    if "phone" in body:
        updates["phone"] = (body.get("phone") or "").strip()[:15] or None
    if not updates:
        return jsonify({"error": "Không có gì để cập nhật"}), 400

    db = get_db()
    sets = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE trackings SET {sets} WHERE id=?", (*updates.values(), tid))
    db.commit()
    row = db.execute("SELECT * FROM trackings WHERE id=?", (tid,)).fetchone()
    return jsonify({"tracking": _row_to_dict(row)})

@app.route("/api/trackings/<int:tid>/refresh", methods=["POST"])
@require_auth
def refresh_tracking(tid):
    row, err = _get_owned_row(tid)
    if err: return jsonify({"error": err[0]}), err[1]
    result = _do_refresh(row, notify=False)
    with _db() as db:
        row = db.execute("SELECT * FROM trackings WHERE id=?", (tid,)).fetchone()
    return jsonify({"tracking": _row_to_dict(row), "fetch": result})

@app.route("/api/trackings/<int:tid>/payload", methods=["GET"])
@require_auth
def tracking_payload(tid):
    row, err = _get_owned_row(tid)
    if err: return jsonify({"error": err[0]}), err[1]
    if not row["last_payload"]:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "raw": json.loads(row["last_payload"]),
        "events": json.loads(row["last_events"] or "[]"),
    })

# ============================================================
# Bootstrap
# ============================================================
init_db()

if os.getenv("DISABLE_BACKGROUND", "0") != "1":
    Thread(target=background_loop, daemon=True).start()
    Thread(target=bot.poll_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
