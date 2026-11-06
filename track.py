#!/usr/bin/env python3
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime

API = "https://tramavandon.com/api/{}.php"


CARRIERS = [
    ("spx", re.compile(r"^SPXVN\d{8,}[A-Z]?$"), False),
    ("lex", re.compile(r"^LEX[A-Z0-9]{8,}$"), False),
    ("ghn", re.compile(r"^(GHN-?[A-Z0-9]{6,}|G[A-Z0-9]{7})$"), True),
    ("jtexpress", re.compile(r"^JT\d{10,}$"), True),
]

RENDERED = [
    ("vnPost", re.compile(r"^[A-Z]{2}\d{9}VN$"), "https://ipostal.vnpost.vn/tra-cuu-buu-gui"),
    ("ViettelPost", re.compile(r"^P[A-Z0-9]{9,}$"),
     "https://viettelpost.vn/viettelpost-iframe/tra-cuu-hanh-trinh-don-hang-v3-recaptcha?orderString={}"),
    ("Ninja Van", re.compile(r"^(NV|NJV)[A-Z0-9]{8,}$"),
     "https://www.ninjavan.co/vi-vn/tracking?tracking_number={}"),
    ("GHTK", re.compile(r"^[A-Z0-9]{8,}$"), "https://i.ghtk.vn/{}"),
]
NO_TRACKING = ("TikTok Express", re.compile(r"^TK[A-Z0-9]{8,}$|^TTS[A-Z0-9]{8,}$"))


def call(carrier, code, phone=None):
    body = {"tracking_id": code}
    if phone:
        body["tracking_phone"] = phone
    req = urllib.request.Request(
        API.format(carrier),
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Origin": "https://tramavandon.com",
            "Referer": "https://tramavandon.com/%s/" % carrier,
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:  
        return json.loads(e.read().decode() or "{}")


def events_of(res):

    data = res.get("data") or {}
    raw = (data.get("sls_tracking_info") or {}).get("records") or data.get("events") or res.get("events") or []
    out = []
    for e in raw:
        if e.get("actual_time"):
            when = datetime.fromtimestamp(e["actual_time"]).strftime("%H:%M %d/%m/%Y")
        else:
            when = ("%s %s" % (e.get("time", ""), e.get("date", ""))).strip()
        desc = e.get("buyer_description") or e.get("description") or e.get("status") or ""
        addr = (e.get("current_location") or {}).get("full_address") or e.get("address") or ""
        out.append((e.get("actual_time") or 0, when, desc, addr))
    out.sort(key=lambda x: x[0], reverse=True)  
    return out


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    argv = [a for a in sys.argv[1:] if a != "--open"]
    do_open = "--open" in sys.argv
    code = argv[0].strip().upper()
    phone = argv[1] if len(argv) > 1 else None

    specific = NO_TRACKING[1].match(code) or any(p.match(code) for _, p, _ in RENDERED[:-1])
    matched = [c for c, pat, _ in CARRIERS if pat.match(code)]
    for carrier in [] if specific else (matched or [c for c, _, _ in CARRIERS]):
        try:
            res = call(carrier, code, phone)
        except Exception as e:
            print("[%s] lỗi: %s" % (carrier, e))
            continue
        if res.get("need_phone") and not phone:
            print("[%s] cần 4 số cuối SĐT: python track.py %s 1234" % (carrier, code))
            continue
        events = events_of(res)
        if not events:
            print("[%s] %s" % (carrier, res.get("message") or "không có dữ liệu"))
            continue

        order = res.get("order") or (res.get("data") or {}).get("order_info") or {}
        status = order.get("status_name") or order.get("tracking_code_group_name") or events[0][2]
        print("Hãng: %s | Mã: %s\nTrạng thái: %s\n" % (carrier.upper(), code, status))
        for _, when, desc, addr in events:
            print("%s  %s" % (when, desc))
            if addr:
                print("   " + addr)
        return

    if NO_TRACKING[1].match(code):
        sys.exit("%s: hãng chưa có kênh tra cứu công khai, không tra được." % NO_TRACKING[0])
    hits = [(name, url) for name, pat, url in RENDERED if pat.match(code)]
    if matched: 
        hits = [h for h in hits if h[0] != "GHTK"]
    if hits:
        name, url = hits[0]
        url = url.format(code)
        print("%s: trang tra cứu cần reCAPTCHA/JS, mở link sau:\n%s" % (name, url))
        if do_open:
            import webbrowser

            webbrowser.open(url)
        return
    sys.exit("Không tra được vận đơn này.")


if __name__ == "__main__":
    main()

