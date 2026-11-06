#!/usr/bin/env python3
"""Tracker core module + CLI.

Dùng như module:
    import track
    data = track.call("spx", "SPXVN...")
    info = track.extract_all(data)

Dùng CLI:
    python track.py SPXVN123456789
    python track.py GHN123456789 0123        # kèm 4 số cuối SĐT
    python track.py CODE --open              # tự mở link nếu cần reCAPTCHA
"""
import json
import re
import sys

import requests

TMVD_BASE = "https://tramavandon.com"
TIMEOUT = 20

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

# Các hãng cần tra cứu qua trang web (reCAPTCHA / JS) thay vì API
MANUAL_LOOKUP = {
    "vnpost":      {"name": "Vietnam Post", "url": "https://ipostal.vnpost.vn/tra-cuu-buu-gui", "match": re.compile(r"^[A-Z]{2}\d{9}VN$")},
    "viettelpost": {"name": "ViettelPost",  "url": "https://viettelpost.vn/viettelpost-iframe/tra-cuu-hanh-trinh-don-hang-v3-recaptcha?orderString={}", "match": re.compile(r"^P[A-Z0-9]{9,}$")},
    "ninjavan":    {"name": "Ninja Van",    "url": "https://www.ninjavan.co/vi-vn/tracking?tracking_number={}", "match": re.compile(r"^(NV|NJV)[A-Z0-9]{8,}$")},
    "ghtk":        {"name": "GHTK",         "url": "https://i.ghtk.vn/{}", "match": re.compile(r"^[A-Z0-9]{8,}$")},
}

DELIVERED_KEYWORDS = [
    "giao hàng thành công", "giao thành công", "đã giao hàng", "đã giao",
    "phát thành công", "đã phát", "giao hang thanh cong", "da giao",
    "phat thanh cong", "hoàn tất", "hoan tat", "đã nhận hàng", "đã nhận",
    "delivered", "delivery success", "delivery successful",
    "completed", "signed", "received",
]

_session = requests.Session()
_session.headers.update(HEADERS)

# ============================================================
# API call
# ============================================================
def call(carrier: str, code: str, phone: str = None) -> dict:
    """Gọi TMVD API. Raise nếu lỗi."""
    meta = CARRIERS[carrier]
    page_url = f"{TMVD_BASE}{meta['page']}?tracking_number={code}"
    api_url  = f"{TMVD_BASE}{meta['api']}"

    r = _session.get(page_url, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()

    body = {"tracking_id": code}
    if phone:
        body["tracking_phone"] = phone

    resp = _session.post(
        api_url, json=body, timeout=TIMEOUT,
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
    "event_time", "eventTime", "occurred_at", "occurredAt",
    "updated_at", "updatedAt", "update_time", "updateTime",
    "created_at", "createdAt", "create_time", "createTime",
    "time", "timestamp", "date", "datetime", "ts",
    "action_time", "actionTime", "action_at", "actionAt",
    "insert_time", "insertTime", "insert_at", "insertAt",
    "record_time", "recordTime", "reported_at", "reportedAt",
    "tracking_time", "trackingTime", "log_time", "logTime",
    "occur_time", "occurTime", "report_time", "reportTime",
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
# Time
# ============================================================
def _normalize_time(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        n = float(v)
        if n > 1e12:
            n /= 1000
        if 1e9 < n < 2_100_000_000:
            try:
                from datetime import datetime, timezone
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
                from datetime import datetime, timezone
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
    if not t:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except Exception:
        return None

def _normalize_event_order(events):
    if len(events) < 2:
        return events
    times = [_parse_time_for_sort(ev.get("time")) for ev in events]
    if all(t is not None for t in times):
        if times[0] <= times[-1]:
            return events
        return list(reversed(events))
    if times[0] is not None and times[-1] is not None:
        if times[0] <= times[-1]:
            return events
        return list(reversed(events))
    return list(reversed(events))

def _extract_event_field(ev, keys, is_code=False, is_time=False):
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

def extract_all(data):
    events = _extract_events(data)
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
# Carrier detection
# ============================================================
_SPX_RE  = re.compile(r"^SPX")
_JT_RE   = re.compile(r"^JT")
_LEX_RE  = re.compile(r"^LEX")
_TTS_RE  = re.compile(r"^TTS|^TK")
_VNP_RE  = re.compile(r"^(EE|CP|RA|VN)\d")
_NJV_RE  = re.compile(r"^NJVN|^SG")
_GHTK_RE = re.compile(r"^S\d{6,}")
_GHN_RE  = re.compile(r"^\d{10,12}$")

def detect_carrier(code: str):
    c = (code or "").strip().upper()
    if not c:
        return None
    if _SPX_RE.search(c):  return "spx"
    if _JT_RE.search(c):   return "jtexpress"
    if _LEX_RE.search(c):  return "lex"
    if _TTS_RE.search(c):  return "tiktok-express"
    if _VNP_RE.search(c):  return "vnpost"
    if _NJV_RE.search(c):  return "ninjavan"
    if _GHTK_RE.search(c): return "giaohangtietkiem"
    if _GHN_RE.search(c):  return "ghn"
    return None

def manual_lookup_for(code: str):
    c = (code or "").strip().upper()
    for key, meta in MANUAL_LOOKUP.items():
        if meta["match"].match(c):
            return {"carrier": key, "name": meta["name"], "url": meta["url"].format(c)}
    return None

# ============================================================
# CLI
# ============================================================
def _fmt_events(events):
    for ev in reversed(events):  # mới → cũ
        t = ev.get("time") or ""
        s = ev.get("status") or ev.get("description") or ""
        print(f"{t}  {s}")
        if ev.get("location"):
            print("   " + ev["location"])

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    argv = [a for a in sys.argv[1:] if a != "--open"]
    do_open = "--open" in sys.argv
    code = argv[0].strip().upper()
    phone = argv[1] if len(argv) > 1 else None

    carrier = detect_carrier(code)
    if not carrier:
        ml = manual_lookup_for(code)
        if ml:
            print(f"{ml['name']}: trang tra cứu cần reCAPTCHA/JS, mở link sau:\n{ml['url']}")
            if do_open:
                import webbrowser
                webbrowser.open(ml["url"])
            return
        sys.exit("Không nhận diện được hãng của mã này.")

    try:
        data = call(carrier, code, phone)
    except Exception as e:
        sys.exit(f"[{carrier}] lỗi: {e}")

    info = extract_all(data)
    if not info.get("status") and not info.get("events"):
        # fallback manual link
        ml = manual_lookup_for(code)
        if ml:
            print(f"[{carrier}] API trả rỗng. Thử mở: {ml['url']}")
            return
        sys.exit(f"[{carrier}] không có dữ liệu.")

    print(f"Hãng: {carrier.upper()} | Mã: {code}")
    print(f"Trạng thái: {info.get('status') or '(null)'}")
    if info.get("product"):
        print(f"Sản phẩm: {info['product']}")
    print()
    _fmt_events(info["events"])


if __name__ == "__main__":
    main()
