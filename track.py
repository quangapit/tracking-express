#!/usr/bin/env python3
"""track.py — TMVD API client + extraction + CLI.

Extraction dùng ĐÚNG cách bản gốc: đọc trực tiếp
    data.sls_tracking_info.records  (hoặc data.events / res.events)
và status từ order.status_name.

Dùng như module:
    import track
    res  = track.call("spx", "SPXVN...")
    info = track.extract_all(res)

CLI:
    python track.py SPXVN123456789
    python track.py JT1234567890 0123       # kèm SĐT
    python track.py CODE --open
"""
import json
import re
import sys
from datetime import datetime, timezone
import urllib.error
import urllib.request

TMVD_BASE = "https://tramavandon.com"
TIMEOUT = 30

# pattern=None → không auto-detect, phải chọn hãng thủ công
CARRIERS = {
    "spx": {
        "name": "Shopee Express",
        "page": "/spx/", "api": "/api/spx.php",
        "pattern": re.compile(r"^SPXVN\d{8,}[A-Z]?$"),
        "need_phone": False,
    },
    "lex": {
        "name": "Lazada Express",
        "page": "/lex/", "api": "/api/lex.php",
        "pattern": re.compile(r"^LEX[A-Z0-9]{8,}$"),
        "need_phone": False,
    },
    "ghn": {
        "name": "Giao Hàng Nhanh",
        "page": "/ghn/", "api": "/api/ghn.php",
        "pattern": re.compile(r"^(GHN-?[A-Z0-9]{6,}|G[A-Z0-9]{7})$"),
        "need_phone": True,
    },
    "jtexpress": {
        "name": "J&T Express",
        "page": "/jtexpress/", "api": "/api/jtexpress.php",
        "pattern": re.compile(r"^JT\d{10,}$"),
        "need_phone": True,
    },
    # Các hãng dưới đây vẫn dùng chung endpoint TMVD
    "giaohangtietkiem": {"name": "Giao Hàng Tiết Kiệm", "page": "/giaohangtietkiem/", "api": "/api/giaohangtietkiem.php", "pattern": None, "need_phone": False},
    "viettelpost":     {"name": "Viettel Post",         "page": "/viettelpost/",     "api": "/api/viettelpost.php",     "pattern": None, "need_phone": False},
    "ninjavan":        {"name": "Ninja Van",            "page": "/ninjavan/",        "api": "/api/ninjavan.php",        "pattern": None, "need_phone": False},
    "tiktok-express":  {"name": "TikTok Express",       "page": "/tiktok-express/",  "api": "/api/tiktok-express.php",  "pattern": None, "need_phone": False},
    "vnpost":          {"name": "Vietnam Post",         "page": "/vnpost/",          "api": "/api/vnpost.php",          "pattern": None, "need_phone": False},
    "ahamove":         {"name": "Ahamove",              "page": "/ahamove/",         "api": "/api/ahamove.php",         "pattern": None, "need_phone": False},
}

DELIVERED_KEYWORDS = [
    "giao hàng thành công", "giao thành công", "đã giao hàng", "đã giao",
    "phát thành công", "đã phát", "giao hang thanh cong", "da giao",
    "phat thanh cong", "hoàn tất", "hoan tat", "đã nhận hàng", "đã nhận",
    "delivered", "delivery success", "delivery successful",
    "completed", "signed", "received",
]

# Fallback: tra qua trang web hãng khi API không chạy được
MANUAL_LOOKUP = [
    ("vnPost",      re.compile(r"^[A-Z]{2}\d{9}VN$"),       "https://ipostal.vnpost.vn/tra-cuu-buu-gui"),
    ("ViettelPost", re.compile(r"^P[A-Z0-9]{9,}$"),         "https://viettelpost.vn/viettelpost-iframe/tra-cuu-hanh-trinh-don-hang-v3-recaptcha?orderString={}"),
    ("Ninja Van",   re.compile(r"^(NV|NJV)[A-Z0-9]{8,}$"),  "https://www.ninjavan.co/vi-vn/tracking?tracking_number={}"),
    ("GHTK",        re.compile(r"^[A-Z0-9]{8,}$"),          "https://i.ghtk.vn/{}"),
]
NO_TRACKING = ("TikTok Express",
               re.compile(r"^TK[A-Z0-9]{8,}$|^TTS[A-Z0-9]{8,}$"))


# ============================================================
# HTTP
# ============================================================
def call(carrier, code, phone=None):
    """POST tới TMVD API. Raise HTTPError nếu lỗi."""
    meta = CARRIERS[carrier]
    body = {"tracking_id": code}
    if phone:
        body["tracking_phone"] = phone
    req = urllib.request.Request(
        TMVD_BASE + meta["api"],
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Origin": TMVD_BASE,
            "Referer": TMVD_BASE + meta["page"],
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode() or "{}")
        except Exception:
            raise


# ============================================================
# Extraction — theo đúng cách bản gốc
# ============================================================
def _records_of(res):
    data = res.get("data") or {}
    return (
        (data.get("sls_tracking_info") or {}).get("records")
        or data.get("events")
        or res.get("events")
        or []
    )


def _order_of(res):
    return res.get("order") or (res.get("data") or {}).get("order_info") or {}


def events_of(res):
    """List event dict, sort CŨ → MỚI (oldest first)."""
    out = []
    for e in _records_of(res):
        if not isinstance(e, dict):
            continue
        ts = e.get("actual_time") or 0
        time_iso = ""
        if ts:
            try:
                time_iso = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
            except Exception:
                time_iso = ""
        if not time_iso:
            t = str(e.get("time") or "").strip()
            d = str(e.get("date") or "").strip()
            time_iso = " ".join(x for x in (t, d) if x).strip()

        desc = (e.get("buyer_description")
                or e.get("description")
                or e.get("status")
                or "")
        addr = ((e.get("current_location") or {}).get("full_address")
                or e.get("address") or "")

        out.append({
            "time":         time_iso,
            "status":       str(desc),
            "description":  "",
            "location":     str(addr),
            "status_code":  str(e.get("status_code") or ""),
            "next_location": "",
            "phase":        "",
            "_ts":          int(ts) if ts else 0,
        })
    out.sort(key=lambda x: (x["_ts"], x["time"]))
    for ev in out:
        ev.pop("_ts", None)
    return out


def get_status(res):
    order = _order_of(res)
    s = order.get("status_name") or order.get("tracking_code_group_name")
    if s:
        return str(s)
    events = events_of(res)
    if events:
        return events[-1]["status"] or None
    return None


def get_product(res):
    order = _order_of(res)
    for k in ("product_name", "productName", "item_name", "itemName",
              "goods_name", "goodsName", "name"):
        v = order.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return None


def is_delivered(status):
    if not status:
        return False
    s = str(status).lower()
    return any(k in s for k in DELIVERED_KEYWORDS)


def extract_all(res):
    """Đóng gói kết quả theo schema app cần."""
    events = events_of(res)
    last = events[-1] if events else {}
    status = get_status(res) or (last.get("status") or None)
    return {
        "status":        status,
        "status_code":   last.get("status_code") or None,
        "phase":         None,
        "location":      last.get("location") or None,
        "next_location": None,
        "event_time":    last.get("time") or None,
        "product":       get_product(res),
        "events":        events,
        "delivered":     is_delivered(status),
    }


# ============================================================
# Detect / fallback
# ============================================================
def detect_carrier(code):
    c = (code or "").strip().upper()
    if not c:
        return None
    for key, meta in CARRIERS.items():
        if meta.get("pattern") and meta["pattern"].match(c):
            return key
    return None


def manual_lookup_for(code):
    c = (code or "").strip().upper()
    if not c:
        return None
    for name, pat, url in MANUAL_LOOKUP:
        if pat.match(c):
            return {"name": name, "url": url.format(c) if "{}" in url else url}
    if NO_TRACKING[1].match(c):
        return {"name": NO_TRACKING[0], "url": None,
                "note": "Hãng chưa có kênh tra cứu công khai."}
    return None


# ============================================================
# CLI
# ============================================================
def _fmt_events(events):
    for ev in reversed(events):  # mới → cũ
        print(f"{ev.get('time') or '—'}  {ev.get('status') or ''}")
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
        if ml and ml.get("url"):
            print(f"{ml['name']}: mở link tra cứu:\n{ml['url']}")
            if do_open:
                import webbrowser
                webbrowser.open(ml["url"])
            return
        if ml and ml.get("note"):
            sys.exit(ml["note"])
        sys.exit("Không nhận diện được hãng của mã này.")

    try:
        data = call(carrier, code, phone)
    except Exception as e:
        sys.exit(f"[{carrier}] lỗi: {e}")

    if data.get("need_phone") and not phone:
        sys.exit(f"[{carrier}] cần 4 số cuối SĐT: python track.py {code} 1234")

    info = extract_all(data)
    if not info.get("status") and not info.get("events"):
        ml = manual_lookup_for(code)
        if ml and ml.get("url"):
            sys.exit(f"[{carrier}] API rỗng. Thử mở: {ml['url']}")
        sys.exit(f"[{carrier}] không có dữ liệu.")

    print(f"Hãng: {carrier.upper()} | Mã: {code}")
    print(f"Trạng thái: {info.get('status') or '(null)'}")
    if info.get("product"):
        print(f"Sản phẩm: {info['product']}")
    print()
    _fmt_events(info["events"])


if __name__ == "__main__":
    main()
