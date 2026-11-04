import os
import re
import json
import time
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from threading import Thread

import requests
from flask import Flask, render_template, request, jsonify, g

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tracker")

app = Flask(__name__)

# ============================================================
# CẤU HÌNH
# ============================================================
TMVD_BASE = os.getenv("TMVD_BASE_URL", "https://tramavandon.com").rstrip("/")
TMVD_TIMEOUT = float(os.getenv("TMVD_REQUEST_TIMEOUT_MS", "20000")) / 1000
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL_SECONDS", "600"))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600"))
DELETE_AFTER_DELIVERED_DAYS = int(os.getenv("DELETE_AFTER_DELIVERED_DAYS", "7"))

DB_DIR = os.getenv("DB_DIR", "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "tracker.db")

CARRIERS = {
    "spx":             {"name": "Shopee Express",       "page": "/spx/",             "api": "/api/spx.php"},
    "ghn":             {"name": "Giao Hàng Nhanh",      "page": "/ghn/",             "api": "/api/ghn.php"},
    "giaohangtietkiem":{"name": "Giao Hàng Tiết Kiệm",  "page": "/giaohangtietkiem/","api": "/api/giaohangtietkiem.php"},
    "jtexpress":       {"name": "J&T Express",          "page": "/jtexpress/",       "api": "/api/jtexpress.php"},
    "viettelpost":     {"name": "Viettel Post",         "page": "/viettelpost/",     "api": "/api/viettelpost.php"},
    "ninjavan":        {"name": "Ninja Van",            "page": "/ninjavan/",        "api": "/api/ninjavan.php"},
    "lex":             {"name": "Lazada Express",       "page": "/lex/",             "api": "/api/lex.php"},
    "tiktok-express":  {"name": "TikTok Express",       "page": "/tiktok-express/",  "api": "/api/tiktok-express.php"},
    "vnpost":          {"name": "Vietnam Post",         "page": "/vnpost/",          "api": "/api/vnpost.php"},
    "ahamove":         {"name": "Ahamove",              "page": "/ahamove/",         "api": "/api/ahamove.php"},
}

HEADERS = {
    "accept": "*/*",
    "accept-language": "en,vi;q=0.9,ru;q=0.8",
    "content-type": "application/json",
    "origin": TMVD_BASE,
    "sec-ch-ua": '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
    ),
    "x-requested-with": "XMLHttpRequest",
}

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

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS trackings (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                carrier          TEXT NOT NULL,
                tracking_code    TEXT NOT NULL,
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
                UNIQUE(carrier, tracking_code)
            );
            CREATE INDEX IF NOT EXISTS idx_trackings_active    ON trackings(is_active);
            CREATE INDEX IF NOT EXISTS idx_trackings_delivered ON trackings(delivered_at);
        """)
        db.commit()
        # Migration: bổ sung cột cho DB cũ
        cols = {r[1] for r in db.execute("PRAGMA table_info(trackings)").fetchall()}
        for col, typ in [
            ("note", "TEXT"),
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

# ============================================================
# Gọi API
# ============================================================
_session = requests.Session()
_session.headers.update(HEADERS)

def _call_tmvd(carrier: str, code: str) -> dict:
    meta = CARRIERS[carrier]
    page_url = f"{TMVD_BASE}{meta['page']}?tracking_number={code}"
    api_url  = f"{TMVD_BASE}{meta['api']}"

    r = _session.get(page_url, timeout=TMVD_TIMEOUT, allow_redirects=True)
    r.raise_for_status()

    resp = _session.post(
        api_url,
        json={"tracking_id": code},
        timeout=TMVD_TIMEOUT,
        headers={"referer": page_url},
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        raise RuntimeError(
            f"API trả về không phải JSON (HTTP {resp.status_code}): {resp.text[:300]!r}"
        )

# ============================================================
# Key pools
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
    "event_code", "eventCode",
    "code",
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
    # ISO / phổ biến
    "event_time", "eventTime", "occurred_at", "occurredAt",
    "updated_at", "updatedAt", "update_time", "updateTime",
    "created_at", "createdAt", "create_time", "createTime",
    "time", "timestamp", "date", "datetime", "ts",
    # SPX / GHN / J&T hay dùng
    "action_time", "actionTime", "action_at", "actionAt",
    "insert_time", "insertTime", "insert_at", "insertAt",
    "record_time", "recordTime", "reported_at", "reportedAt",
    "tracking_time", "trackingTime", "log_time", "logTime",
    "occur_time", "occurTime", "report_time", "reportTime",
    # Dạng chuỗi
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
)
EVENT_DESC_KEYS = (
    "description", "desc", "message", "msg", "note", "detail", "content",
)

# ============================================================
# Envelope detection (fix "success" / "R00")
# ============================================================
_ENVELOPE_VALUES = {
    "success", "ok", "error", "true", "false", "fail", "failed",
    "1", "0", "null", "none", "yes", "no",
}
_ENVELOPE_CODES = {
    "R00", "00", "000", "200", "201", "OK", "SUCCESS",
    "ERROR", "FAIL", "FAILED",
}

def _is_envelope_value(v):
    if v is None:
        return True
    return str(v).strip().lower() in _ENVELOPE_VALUES

def _is_envelope_code(v):
    if v is None:
        return True
    return str(v).strip().upper() in _ENVELOPE_CODES

def _unwrap(data):
    """Bóc envelope {status, code, data: {...}} → payload thật bên trong."""
    if isinstance(data, dict):
        for k in ("data", "result", "response", "payload", "tracking_data"):
            v = data.get(k)
            if isinstance(v, (dict, list)) and v:
                return v
    return data

# ============================================================
# Walk helpers
# ============================================================
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
    """Fallback: list-of-dicts bất kỳ có item trông giống event."""
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
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        for k in ("name", "text", "label", "title", "description",
                  "desc", "content", "value", "status"):
            if k in v:
                inner = _extract_string(v[k])
                if inner:
                    return inner
    return None

# ============================================================
# Time normalisation
# ============================================================
def _normalize_time(v):
    """Chuẩn hoá nhiều định dạng thời gian → chuỗi dễ đọc.
    - int/float Unix giây hoặc mili
    - str: chuỗi số → ISO; chuỗi khác → giữ nguyên
    - dict: dò key time bên trong
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        n = float(v)
        if n > 1e12:
            n /= 1000
        if 1e9 < n < 2_100_000_000:
            try:
                return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
            except Exception:
                pass
        return str(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if s.isdigit():
            try:
                n = float(s)
                if n > 1e12:
                    n /= 1000
                if 1e9 < n < 2_100_000_000:
                    return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
            except Exception:
                pass
        return s
    if isinstance(v, dict):
        for k in TIME_KEYS:
            if k in v:
                inner = _normalize_time(v[k])
                if inner:
                    return inner
    return None


def _parse_time_for_sort(t):
    """Parse thời gian để sort. Trả None nếu không parse được."""
    if not t:
        return None
    try:
        # ISO 8601: "2026-10-30T09:56:00+07:00" hoặc "...Z"
        return datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except Exception:
        return None


def _normalize_event_order(events):
    """Chuẩn hoá mảng events về CŨ → MỚI (chronological).
    - Có time parse được → sort tăng dần.
    - Không đủ time → giả định API trả mới→cũ (SPX, GHN, J&T), đảo lại.
    """
    if len(events) < 2:
        return events

    times = [_parse_time_for_sort(ev.get("time")) for ev in events]

    # Trường hợp 1: tất cả time parse được → sort chính xác
    if all(t is not None for t in times):
        if times[0] <= times[-1]:
            return events                       # đã cũ→mới
        return list(reversed(events))           # mới→cũ → đảo

    # Trường hợp 2: chỉ biết first và last
    if times[0] is not None and times[-1] is not None:
        if times[0] <= times[-1]:
            return events
        return list(reversed(events))

    # Trường hợp 3: không có time → giả định mới→cũ (theo API), đảo lại
    return list(reversed(events))


def _extract_event_field(ev, keys, is_code=False, is_time=False):
    """Đọc 1 field từ event.
    - Ưu tiên key top-level của event (event thường phẳng).
    - is_code: bỏ qua envelope codes (R00/00/OK).
    - is_time: chuẩn hoá qua _normalize_time.
    """
    if isinstance(ev, dict):
        for k in keys:
            if k in ev and ev[k] not in (None, "", []):
                val = ev[k]
                if is_time:
                    t = _normalize_time(val)
                    if t:
                        return t
                    continue
                s = _extract_string(val)
                if not s:
                    continue
                if is_code and _is_envelope_code(s):
                    continue
                return s
    # Fallback: walk sâu
    v = _walk_values(ev, keys)
    if is_time:
        return _normalize_time(v)
    s = _extract_string(v)
    if is_code and s and _is_envelope_code(s):
        return None
    return s

# ============================================================
# Extraction
# ============================================================
def _extract_events(data):
    events = _find_list_by_keys(data, EVENT_LIST_KEYS) or _find_events_anywhere(data)
    if not events:
        return []
    out = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        status   = _extract_event_field(ev, STATUS_KEYS)
        code     = _extract_event_field(ev, STATUS_CODE_KEYS, is_code=True)
        phase    = _extract_event_field(ev, PHASE_KEYS)
        location = _extract_event_field(ev, LOCATION_KEYS)
        next_loc = _extract_event_field(ev, NEXT_LOCATION_KEYS)
        time_    = _extract_event_field(ev, TIME_KEYS, is_time=True)
        desc     = _extract_event_field(ev, EVENT_DESC_KEYS)
        if any((status, code, phase, location, next_loc, time_, desc)):
            out.append({
                "status": status or "",
                "status_code": code or "",
                "phase": phase or "",
                "location": location or "",
                "next_location": next_loc or "",
                "time": time_ or "",
                "description": desc or "",
            })
    return _normalize_event_order(out)


def _pick_status(root):
    for key_group in (
        ("status_text", "statusText", "status_name", "statusName",
         "status_description", "statusDescription", "status_desc"),
        ("current_status", "currentStatus", "current_state", "currentState",
         "shipping_status", "delivery_status", "deliveryStatus"),
        ("order_status", "orderStatus", "last_status"),
        ("state_text", "stateText", "state"),
        ("description", "desc", "message", "msg"),
        ("status",),
    ):
        v = _walk_values(root, key_group)
        s = _extract_string(v)
        if s and not _is_envelope_value(s):
            return s
    return None


def _pick_code(root):
    return _extract_event_field(root, STATUS_CODE_KEYS, is_code=True)


def _pick_phase(root):
    v = _walk_values(root, PHASE_KEYS)
    s = _extract_string(v)
    if s and not _is_envelope_value(s):
        return s
    return None


def _is_delivered(status_text):
    if not status_text:
        return False
    s = status_text.lower()
    return any(k in s for k in DELIVERED_KEYWORDS)


def _extract_all(data):
    events = _extract_events(data)
    # events đã được chuẩn hoá về cũ→mới → event cuối là mới nhất
    last_ev = events[-1] if events else {}

    root = _unwrap(data)

    status      = (last_ev.get("status")      or _pick_status(root))
    status_code = (last_ev.get("status_code") or _pick_code(root))
    phase       = (last_ev.get("phase")       or _pick_phase(root))
    location    = (last_ev.get("location")
                   or _extract_string(_walk_values(root, LOCATION_KEYS)))
    next_loc    = (last_ev.get("next_location")
                   or _extract_string(_walk_values(root, NEXT_LOCATION_KEYS)))
    event_time  = (last_ev.get("time")
                   or _extract_string(_walk_values(root, TIME_KEYS)))
    product     = _extract_string(_walk_values(root, PRODUCT_KEYS))

    return {
        "status": status,
        "status_code": status_code,
        "phase": phase,
        "location": location,
        "next_location": next_loc,
        "event_time": event_time,
        "product": product,
        "events": events,
        "delivered": _is_delivered(status),
    }

# ============================================================
# Nghiệp vụ
# ============================================================
def refresh_one(row):
    if not row["is_active"]:
        return {"status": row["last_status"], "skipped": True}

    carrier, code = row["carrier"], row["tracking_code"]
    try:
        data = _call_tmvd(carrier, code)
    except Exception as e:
        log.warning("refresh %s/%s lỗi: %s", carrier, code, e)
        return {"error": str(e)}

    info = _extract_all(data)
    now = datetime.now(timezone.utc).isoformat()

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
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=DELETE_AFTER_DELIVERED_DAYS)).isoformat()
    db = get_db()
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
                rows = db.execute("SELECT * FROM trackings WHERE is_active = 1").fetchall()
                log.info("background: refresh %d bản ghi", len(rows))
                for row in rows:
                    refresh_one(row)
                now_ts = time.time()
                if now_ts - last_cleanup > CLEANUP_INTERVAL:
                    cleanup_old()
                    last_cleanup = now_ts
        except Exception as e:
            log.exception("background loop lỗi: %s", e)
        time.sleep(REFRESH_INTERVAL)

# ============================================================
# Auto-detect
# ============================================================
_SPX_RE  = re.compile(r"^SPX")
_JT_RE   = re.compile(r"^JT")
_LEX_RE  = re.compile(r"^LEX")
_TTS_RE  = re.compile(r"^TTS|^TK")
_VNP_RE  = re.compile(r"^(EE|CP|RA|VN)\d")
_NJV_RE  = re.compile(r"^NJVN|^SG")
_GHTK_RE = re.compile(r"^S\d{6,}")
_GHN_RE  = re.compile(r"^\d{10,12}$")

def detect_carrier(code):
    c = (code or "").strip().upper()
    if not c: return None
    if _SPX_RE.search(c):  return "spx"
    if _JT_RE.search(c):   return "jtexpress"
    if _LEX_RE.search(c):  return "lex"
    if _TTS_RE.search(c):  return "tiktok-express"
    if _VNP_RE.search(c):  return "vnpost"
    if _NJV_RE.search(c):  return "ninjavan"
    if _GHTK_RE.search(c): return "giaohangtietkiem"
    if _GHN_RE.search(c):  return "ghn"
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
    return d

# ============================================================
# Routes
# ============================================================
@app.route("/")
def index():
    return render_template("index.html", carriers=CARRIERS)

@app.route("/api/carriers")
def list_carriers():
    return jsonify({k: v["name"] for k, v in CARRIERS.items()})

@app.route("/api/detect", methods=["POST"])
def api_detect():
    code = (request.get_json(force=True).get("tracking_code") or "").strip()
    return jsonify({"carrier": detect_carrier(code)})

@app.route("/api/raw/<carrier>/<code>")
def api_raw(carrier, code):
    if carrier not in CARRIERS:
        return jsonify({"error": f"Hãng không hỗ trợ: {carrier}"}), 400
    try:
        data = _call_tmvd(carrier, code.upper())
    except Exception as e:
        return jsonify({"ok": False, "carrier": carrier,
                        "code": code.upper(), "error": str(e)}), 500
    return jsonify({
        "ok": True,
        "carrier": carrier,
        "code": code.upper(),
        "carrier_name": CARRIERS[carrier]["name"],
        "extracted": _extract_all(data),
        "raw": data,
    })

@app.route("/api/trackings", methods=["GET"])
def list_trackings():
    cleanup_old()
    db = get_db()
    rows = db.execute("SELECT * FROM trackings ORDER BY id DESC").fetchall()
    return jsonify([_row_to_dict(r) for r in rows])

@app.route("/api/trackings", methods=["POST"])
def add_tracking():
    body = request.get_json(force=True) or {}
    code = (body.get("tracking_code") or "").strip().upper()
    carrier = (body.get("carrier") or "").strip().lower()
    note = (body.get("note") or "").strip()[:200] or None

    if not code:
        return jsonify({"error": "Thiếu mã vận đơn"}), 400
    if not carrier:
        carrier = detect_carrier(code)
        if not carrier:
            return jsonify({"error": "Không nhận diện được hãng. Vui lòng chọn hãng thủ công."}), 400
    if carrier not in CARRIERS:
        return jsonify({"error": f"Hãng không hỗ trợ: {carrier}"}), 400

    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    try:
        cur = db.execute(
            "INSERT INTO trackings (carrier, tracking_code, note, created_at) VALUES (?,?,?,?)",
            (carrier, code, note, now),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Mã này đã được theo dõi"}), 409

    row = db.execute("SELECT * FROM trackings WHERE id = ?", (cur.lastrowid,)).fetchone()
    fetch_result = refresh_one(row)
    row = db.execute("SELECT * FROM trackings WHERE id = ?", (row["id"],)).fetchone()
    return jsonify({"tracking": _row_to_dict(row), "fetch": fetch_result}), 201

@app.route("/api/trackings/<int:tid>", methods=["DELETE"])
def delete_tracking(tid):
    db = get_db()
    db.execute("DELETE FROM trackings WHERE id = ?", (tid,))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/trackings/<int:tid>", methods=["PATCH"])
def patch_tracking(tid):
    body = request.get_json(force=True) or {}
    if "note" not in body:
        return jsonify({"error": "Không có trường nào để cập nhật"}), 400
    note = (body.get("note") or "").strip()[:200] or None
    db = get_db()
    db.execute("UPDATE trackings SET note = ? WHERE id = ?", (note, tid))
    db.commit()
    row = db.execute("SELECT * FROM trackings WHERE id = ?", (tid,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify({"tracking": _row_to_dict(row)})

@app.route("/api/trackings/<int:tid>/refresh", methods=["POST"])
def refresh_tracking(tid):
    db = get_db()
    row = db.execute("SELECT * FROM trackings WHERE id = ?", (tid,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    result = refresh_one(row)
    row = db.execute("SELECT * FROM trackings WHERE id = ?", (tid,)).fetchone()
    return jsonify({"tracking": _row_to_dict(row), "fetch": result})

@app.route("/api/trackings/<int:tid>/payload", methods=["GET"])
def tracking_payload(tid):
    db = get_db()
    row = db.execute(
        "SELECT last_payload, last_events FROM trackings WHERE id = ?", (tid,)
    ).fetchone()
    if not row or not row["last_payload"]:
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
