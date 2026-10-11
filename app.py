import os
import re
import csv
import io
import json
import uuid
import time
import random
import imaplib
import email
import datetime
import threading
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from flask import Flask, request, jsonify, render_template_string, Response

app = Flask(__name__)

# ================= কনফিগারেশন =================
CONFIG = {
    "GMAIL_USER": "tanmaykundu439@gmail.com",
    "GMAIL_PASS": "cjmjsbslgwdzbfrw",
    "UPI_ID": "tanmaykundu439@yesfam",
    "MERCHANT_NAME": "BN Enterprises",
    "STRICT_CODE": False,       # 🔓 False = শিথিল মোড
    "SECURITY_SWITCH": False,   # 🚨 চার্জার অ্যালার্ম
    "SECURITY_PIN": "1234"
}

ORDER_TIMEOUT = 300
EMAIL_FETCH_COUNT = 20
EMAIL_LOOKBACK_MIN = 30
SUCCESS_SHOW_SEC = 20        # ডিসপ্লেতে success কতক্ষণ দেখাবে

HISTORY_FILE = "payment_history.json"
ORDERS_FILE = "orders_state.json"    # 💾 সব worker একসাথে এটা share করবে

DEBUG_LOG = []
ALARM_ACK = 0

_state_lock = threading.Lock()

def _log_debug(order_id, reason):
    DEBUG_LOG.append({"time": datetime.datetime.now().strftime("%H:%M:%S"),
                      "order_id": str(order_id), "reason": str(reason)[:100]})
    del DEBUG_LOG[:-40]

# ================= 💾 File-backed State (Cross-Device Fix) =================
def read_orders():
    """সব worker একই ফাইল পড়বে — তাই এক device-এর order আরেক device-এ দেখা যাবে"""
    with _state_lock:
        try:
            with open(ORDERS_FILE) as f:
                d = json.load(f)
            return (d.get("current_order_id"),
                    d.get("orders", {}),
                    set(d.get("used_utrs", [])),
                    set(d.get("used_emails", [])))
        except Exception:
            return None, {}, set(), set()

def write_orders(cur, orders, used_utrs, used_emails):
    with _state_lock:
        try:
            with open(ORDERS_FILE, "w") as f:
                json.dump({
                    "current_order_id": cur,
                    "orders": orders,
                    "used_utrs": list(used_utrs)[-200:],
                    "used_emails": list(used_emails)[-300:]
                }, f)
        except Exception:
            pass

def get_history():
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def append_history(recs):
    if not recs:
        return
    try:
        hist = get_history()
        known = {t.get("order_id") for t in hist}
        new = [r for r in recs if r.get("order_id") not in known]
        if new:
            hist.extend(new)
            with open(HISTORY_FILE, "w") as f:
                json.dump(hist, f)
    except Exception:
        pass

def cleanup_orders_dict(orders):
    """মেয়াদোত্তীর্ণ অর্ডার EXPIRED করে — রেকর্ড ফেরত দেয়"""
    now = time.time()
    changed = False
    recs = []
    for oid, o in list(orders.items()):
        if o.get("status") == "PENDING" and now - o.get("created_at", now) > ORDER_TIMEOUT:
            o["status"] = "EXPIRED"
            rec = {
                "order_id": oid,
                "secret_code": o["secret_code"],
                "amount": float(o["amount"]),
                "status": "EXPIRED",
                "payer": None,
                "utr": "—",
                "time": datetime.datetime.now().strftime("%I:%M:%S %p"),
                "date": datetime.datetime.now().strftime("%Y-%m-%d")
            }
            o["record"] = rec
            recs.append(rec)
            changed = True
    return changed, recs

# ================= ⚡ Gmail (Persistent + UID Cache) =================
_gmail_lock = threading.Lock()
_imap = None
_uid_cache = {}

def _get_imap():
    global _imap
    if _imap is not None:
        try:
            _imap.noop()
            return _imap
        except Exception:
            try:
                _imap.logout()
            except Exception:
                pass
            _imap = None
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(CONFIG["GMAIL_USER"], CONFIG["GMAIL_PASS"])
    _imap = m
    return m

def _parse_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body += payload.decode(errors="ignore")
                except Exception:
                    pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            body = payload.decode(errors="ignore") if payload else str(msg.get_payload())
        except Exception:
            body = str(msg.get_payload())

    if "<html" in body.lower():
        body = re.sub(r"<style.*?</style>", " ", body, flags=re.S | re.I)
        body = re.sub(r"<script.*?</script>", " ", body, flags=re.S | re.I)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body)
    return body

def _decode_subject(msg):
    try:
        decoded = email.header.decode_header(msg.get("Subject", ""))
        subject = ""
        for s, enc in decoded:
            if isinstance(s, bytes):
                subject += s.decode(enc or "utf-8", errors="ignore")
            else:
                subject += str(s)
        return subject
    except Exception:
        return str(msg.get("Subject", ""))

_last_fetch = 0

def _fetch_emails_once():
    global _last_fetch, _imap
    try:
        with _gmail_lock:
            m = _get_imap()
            m.select("inbox")
            since = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
            _, messages = m.uid('search', None, f'(SINCE "{since}")')
            uids = messages[0].split()[-EMAIL_FETCH_COUNT:]

            for uid in reversed(uids):
                u = uid.decode()
                if u in _uid_cache:
                    continue
                try:
                    _, data = m.uid('fetch', uid, '(RFC822)')
                    for part in data:
                        if isinstance(part, tuple):
                            msg = email.message_from_bytes(part[1])
                            _uid_cache[u] = (msg, _decode_subject(msg), _parse_body(msg))
                            break
                except Exception:
                    continue

            if len(_uid_cache) > 40:
                keep = set(x.decode() for x in uids)
                for k in list(_uid_cache.keys()):
                    if k not in keep:
                        del _uid_cache[k]

            _last_fetch = time.time()
            return True
    except Exception as e:
        _log_debug("poller", e)
        try:
            if _imap:
                _imap.logout()
        except Exception:
            pass
        _imap = None
        return False

def email_poller():
    fails = 0
    while True:
        ok = _fetch_emails_once()
        if ok:
            fails = 0
            time.sleep(2)
        else:
            fails += 1
            time.sleep(min(3 + fails, 10))

threading.Thread(target=email_poller, daemon=True).start()

def get_recent_emails():
    return list(_uid_cache.values())

def test_gmail_connection():
    if _last_fetch and (time.time() - _last_fetch) < 25:
        return True, "জিমেইল কানেক্টেড"
    if _fetch_emails_once():
        return True, "জিমেইল কানেক্টেড"
    return False, "জিমেইল কানেকশন সমস্যা — অপেক্ষা করুন"

# ================= Payer Name Extractor =================
def extract_payer_name(subject, body):
    text = re.sub(r"\s+", " ", f"{subject} {body}")
    text = text.replace("*", " ").replace("#", " ").replace("_", " ")

    STOP = (r"(?=\s*(?:\(|\[|\bvia\b|\bref\b|\butr\b|\brrn\b|\btid\b|\btxn\b|\bid\b|\bno\b|\bon\b|"
            r"\btowards\b|\bfor\b|\bfrom\b|\bwith\b|\byour\b|\bupi\b|\bacc\b|\baccount\b|\ba/c\b|"
            r"\bbank\b|\bto\b|\bbalance\b|\bwallet\b|[,.\-|!?:;]|\d|$))")

    patterns = [
        rf"(?:received|got|credited(?:\s+with)?)\s*(?:₹|rs\.?|inr)?\s*[\d,]+(?:\.\d{{1,2}})?\s*"
        rf"(?:from|by)\s+((?:mr\.?|mrs\.?|ms\.?|md\.?)?\s*[A-Za-z][A-Za-z\s\.']{{1,39}}?){STOP}",
        rf"([A-Za-z][A-Za-z\s\.']{{1,39}}?)\s+sent\s+you{STOP}",
        rf"(?:₹|rs\.?|inr)\s*[\d,]+(?:\.\d{{1,2}})?\s*(?:received|credited)\s*from\s+"
        rf"([A-Za-z][A-Za-z\s\.']{{1,39}}?){STOP}",
        rf"from\s+((?:mr\.?|mrs\.?|ms\.?|md\.?)?\s*[A-Za-z][A-Za-z\s\.']{{1,39}}?){STOP}",
        r"(?:payer|sender|customer|remitter|beneficiary)\s*name\s*[:\-]\s*([A-Za-z][A-Za-z\s\.']{2,39})",
        rf"(?:payment|paid|transfer(?:red)?)\s+by\s+([A-Za-z][A-Za-z\s\.']{{1,39}}?){STOP}",
    ]

    not_name_words = {
        "upi", "ref", "inr", "rs", "your", "the", "this", "a/c", "ac", "bank", "http", "www",
        "you", "yours", "dear", "hey", "hi", "hello", "fam", "fampay", "fampay!", "noreply",
        "team", "txn", "transaction", "payment", "money", "credited", "received", "id",
        "no", "reply", "support", "user", "customer", "account", "wallet", "balance"
    }

    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            name = m.group(1).strip(" .,|-_'\"")
            name = re.sub(r"\s+", " ", name)
            if name.isupper():
                name = name.title()
            first_word = name.split()[0].lower() if name else ""
            if (len(name) >= 3
                    and first_word not in not_name_words
                    and not name.isdigit()
                    and not name.upper().startswith("FP")
                    and "@" not in name):
                return name
    return None

def _is_recent_mail(msg):
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        if not dt:
            return True
        age = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
        return -180 < age < EMAIL_LOOKBACK_MIN * 60
    except Exception:
        return True

# ================= ✅ পেমেন্ট ভেরিফিকেশন =================
AMOUNT_PATTERNS = [
    r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)",
    r"([\d,]+(?:\.\d{1,2})?)\s*(?:rs\.?|inr|/-)",
]
CREDIT_WORDS = ["credited", "received", "deposited", "added", "got"]
DEBIT_WORDS = ["debited", "sent to", "paid to", "withdrawn"]

def verify_payment_mail(secret_code, expected_amount, created_at=None, strict=True, used_emails=None):
    emails = get_recent_emails()
    target = str(secret_code).strip().lower()
    expected = round(float(expected_amount), 2)
    if used_emails is None:
        used_emails = set()

    if not emails:
        return False, None, None, "no_emails"

    last_reason = "no_match"

    for msg, subject, body in emails:
        if not _is_recent_mail(msg):
            continue

        try:
            mid = (msg.get("Message-ID") or (subject + str(msg.get("Date")))).strip()
        except Exception:
            mid = subject + str(msg.get("Date"))
        if mid in used_emails:
            last_reason = "email_used"
            continue

        if created_at is not None:
            try:
                dt = parsedate_to_datetime(msg.get("Date"))
                if dt and dt.timestamp() < (created_at - 180):
                    last_reason = "too_old"
                    continue
            except Exception:
                pass

        text = f"{subject} {body}"
        low = text.lower()

        if any(w in low for w in DEBIT_WORDS) and not any(w in low for w in CREDIT_WORDS):
            continue

        if not any(w in low for w in CREDIT_WORDS) and "upi" not in low:
            continue

        code_found = target in low

        all_amounts = []
        for pat in AMOUNT_PATTERNS:
            all_amounts += re.findall(pat, text, re.IGNORECASE)

        amount_match = False
        for a in all_amounts:
            try:
                val = float(a.replace(",", ""))
            except ValueError:
                continue
            if abs(expected - val) < 0.01:
                amount_match = True
                break

        if not amount_match:
            last_reason = "no_amount_match"
            continue

        if strict and not code_found:
            last_reason = "no_code_match"
            continue

        m = re.search(
            r"(?:ref(?:\s*(?:no|number)?)?|rrn|utr|upi\s*ref)[:\s./]*([0-9]{10,14})",
            text, re.IGNORECASE)
        utr = m.group(1) if m else ("UPI-" + secret_code)
        payer = extract_payer_name(subject, body)
        used_emails.add(mid)
        return True, utr, payer, "matched"

    return False, None, None, last_reason

# ================= UI (সম্পূর্ণ বাংলা) =================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>বিএন এন্টারপ্রাইজেস</title>
<script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
<style>
*{box-sizing:border-box}
:root{--bg:#f1f5f9;--card:#fff;--primary:#2563eb;--primary-dark:#1d4ed8;--success:#16a34a;--danger:#dc2626;--text:#0f172a;--muted:#64748b;--border:#e2e8f0}
html,body{height:100%}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);margin:0;padding:20px 14px 70px}
.container{max-width:1000px;margin:0 auto;display:flex;flex-direction:column;gap:18px}

.header-card{background:linear-gradient(-45deg,#1e3a8a,#2563eb,#0ea5e9,#1d4ed8);background-size:400% 400%;animation:gradientShift 10s ease infinite;border-radius:18px;padding:22px 26px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:14px;color:#fff;box-shadow:0 12px 30px -10px rgba(37,99,235,.55)}
.merchant-title{display:flex;align-items:center;gap:14px}
.merchant-logo{width:52px;height:52px;background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.35);border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:26px;animation:floatY 3s ease-in-out infinite}
.badge-group{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.badge{padding:7px 14px;border-radius:9999px;font-size:13px;font-weight:600;display:inline-flex;align-items:center;gap:6px}
.badge-glass{background:rgba(255,255,255,.16);color:#fff;border:1px solid rgba(255,255,255,.32);backdrop-filter:blur(6px)}
.badge-online{background:#dcfce7;color:#15803d}
.badge-offline{background:#fee2e2;color:#b91c1c}
.live-clock{font-variant-numeric:tabular-nums}
.live-dot{width:8px;height:8px;background:#4ade80;border-radius:50%;animation:pulse 1.4s infinite}

.tabs{display:flex;gap:6px;background:var(--card);padding:6px;border-radius:14px;border:1px solid var(--border);width:fit-content;box-shadow:0 4px 6px -1px rgba(0,0,0,.05)}
.tab{border:none;background:transparent;padding:10px 22px;border-radius:10px;font-weight:600;cursor:pointer;color:var(--muted);transition:.25s;font-size:14px}
.tab.active{background:var(--primary);color:#fff;box-shadow:0 4px 12px -2px rgba(37,99,235,.5)}
.tab-content{display:none;flex-direction:column;gap:18px}
.tab-content.active{display:flex;animation:fadeInUp .45s ease}

.grid-layout{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:768px){.grid-layout{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:22px;box-shadow:0 4px 6px -1px rgba(0,0,0,.05)}
.anim{opacity:0;animation:fadeInUp .55s ease forwards}
.d1{animation-delay:.05s}.d2{animation-delay:.15s}.d3{animation-delay:.25s}

h3{margin:0 0 16px;font-size:17px}
label{font-size:13px;font-weight:600;color:var(--muted);display:block;margin-bottom:6px}
input[type=text],input[type=number]{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--border);background:#f8fafc;font-size:15px;margin-bottom:14px;outline:none;transition:.2s}
input:focus{border-color:var(--primary);background:#fff;box-shadow:0 0 0 3px rgba(37,99,235,.12)}
.btn{background:var(--primary);color:#fff;border:none;padding:13px 20px;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;width:100%;transition:.2s}
.btn:hover{background:var(--primary-dark);transform:translateY(-1px)}
.btn:disabled{background:#94a3b8;cursor:not-allowed;transform:none}
.btn.gray{background:#64748b;margin-top:12px}
.btn.red{background:var(--danger);margin-top:12px}
.btn.dark{background:#334155}
.btn.orange{background:#d97706}
.btn.small{width:auto;padding:9px 16px;font-size:13px}
.toggle-row{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--text);margin:2px 0 14px;cursor:pointer}
.toggle-row input[type=checkbox]{width:auto;margin:0;accent-color:var(--primary)}

.quick-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.quick-btn{flex:1;min-width:60px;padding:9px 0;border:1px solid var(--border);background:#f8fafc;border-radius:9px;font-weight:700;font-size:13px;cursor:pointer;color:var(--text);transition:.2s}
.quick-btn:hover{border-color:var(--primary);color:var(--primary);background:#eff6ff}

.qr-display{text-align:center}
#qr-box{padding:14px;background:#fff;border:2px dashed #cbd5e1;border-radius:14px;display:inline-block;margin:10px auto;animation:qrPop .55s cubic-bezier(.34,1.56,.64,1)}

.status-pill{font-size:14.5px;font-weight:600;padding:11px;border-radius:9px;margin-top:10px;display:flex;align-items:center;justify-content:center;gap:8px}
.status-waiting{background:#fef9c3;color:#854d0e;animation:pulse 1.6s infinite}
.status-expired{background:#fee2e2;color:#b91c1c}
.dots span{width:6px;height:6px;background:currentColor;border-radius:50%;display:inline-block;animation:bounceDot 1.2s infinite}
.dots span:nth-child(2){animation-delay:.15s}
.dots span:nth-child(3){animation-delay:.3s}

.timer-wrap{height:8px;background:#e2e8f0;border-radius:99px;overflow:hidden;margin-top:12px}
#timer-bar{height:100%;width:100%;background:linear-gradient(90deg,#16a34a,#84cc16);border-radius:99px;transition:width 1s linear,background .6s}
#timer-bar.low{background:linear-gradient(90deg,#dc2626,#f97316)}
#timer-text{font-size:12px;color:var(--muted);margin-top:6px;font-variant-numeric:tabular-nums}

.checkmark{width:74px;height:74px;display:block;margin:6px auto 2px}
.checkmark circle{stroke:var(--success);stroke-width:3;fill:none;stroke-dasharray:166;stroke-dashoffset:166;animation:drawStroke .6s cubic-bezier(.65,0,.45,1) forwards}
.checkmark path{stroke:var(--success);stroke-width:4;fill:none;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:48;stroke-dashoffset:48;animation:drawStroke .35s .55s cubic-bezier(.65,0,.45,1) forwards}

.thank-card{text-align:center;background:linear-gradient(135deg,#ecfdf5,#d1fae5);border:1px solid #a7f3d0;border-radius:16px;padding:18px 14px;margin:14px 0 10px;animation:fadeInUp .5s ease}
.thank-title{font-size:13px;color:#059669;font-weight:800;letter-spacing:1px}
.thank-name{font-size:28px;font-weight:900;color:#065f46;margin:4px 0;animation:namePop .8s cubic-bezier(.34,1.56,.64,1);word-break:break-word}
.thank-name .at{color:#10b981}
.thank-msg{font-size:13.5px;font-weight:700;color:#047857}
.thank-amount-box{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px;margin-bottom:12px}
.thank-label{font-size:11px;color:var(--muted);font-weight:800;letter-spacing:1px}
.thank-amount{font-size:40px;font-weight:900;color:var(--success);font-variant-numeric:tabular-nums;line-height:1.15}
.thank-time{font-size:13px;color:var(--muted);font-weight:600;margin-top:4px}
@keyframes namePop{0%{transform:scale(.4);opacity:0}60%{transform:scale(1.15)}100%{transform:scale(1);opacity:1}}

/* 🖥️ কাস্টমার ডিসপ্লে */
body.display-mode{background:#04281e;overflow:hidden;touch-action:none;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
#display-app{display:none;position:fixed;inset:0;flex-direction:column}
body.display-mode #display-app{display:flex}
body.display-mode #pos-app{display:none}

.dsp-activate{position:fixed;inset:0;background:#04281e;z-index:10050;display:flex;flex-direction:column;align-items:center;justify-content:center;color:#fff;text-align:center;gap:16px;padding:24px;cursor:pointer}
.dsp-activate.hide{display:none}
.dsp-logo{font-size:52px;animation:floatY 2.5s ease-in-out infinite}
.dsp-name{font-size:26px;font-weight:900}
.dsp-tap{background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.35);padding:16px 34px;border-radius:9999px;font-weight:800;font-size:17px;animation:pulse 1.6s infinite}

.dsp-screen{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;color:#fff;padding:24px;background:linear-gradient(-45deg,#064e3b,#059669,#10b981,#047857);background-size:400% 400%;animation:gradientShift 10s ease infinite;transition:.4s}
.dsp-screen.idle{background:linear-gradient(-45deg,#1e293b,#334155,#475569,#334155);background-size:400% 400%}
.dsp-merchant{position:fixed;top:20px;left:0;right:0;text-align:center;font-size:15px;font-weight:800;opacity:.8;letter-spacing:1px;pointer-events:none;z-index:5}

.bubble-wrap{position:relative;width:130px;height:130px;margin-bottom:8px}
.bubble{position:absolute;border-radius:50%;animation:bubbleFloat 4s ease-in-out infinite}
.b1{width:112px;height:112px;left:9px;top:9px;background:rgba(255,255,255,.07);border:1.5px solid rgba(255,255,255,.16)}
.b2{width:42px;height:42px;right:-6px;top:-2px;background:rgba(74,222,128,.16);border:1px solid rgba(74,222,128,.35);animation-delay:.8s}
.b3{width:26px;height:26px;left:-4px;bottom:8px;background:rgba(96,165,250,.2);border:1px solid rgba(96,165,250,.4);animation-delay:1.6s}
.b4{width:16px;height:16px;right:14px;bottom:-4px;background:rgba(251,191,36,.22);border:1px solid rgba(251,191,36,.4);animation-delay:2.2s}
.dsp-idle-logo{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:54px;animation:floatY 3s ease-in-out infinite;z-index:2}
.dsp-idle-txt{font-size:25px;font-weight:800;margin-top:16px}
.dsp-idle-sub{font-size:14px;opacity:.6;margin-top:7px;font-weight:600}
.dsp-idle-pulse{display:flex;gap:9px;margin-top:24px}
.dsp-idle-pulse span{width:11px;height:11px;border-radius:50%;background:rgba(255,255,255,.75);animation:bounceDot 1.2s infinite}
.dsp-idle-pulse span:nth-child(2){animation-delay:.15s}
.dsp-idle-pulse span:nth-child(3){animation-delay:.3s}
@keyframes bubbleFloat{0%,100%{transform:translateY(0) scale(1)}50%{transform:translateY(-14px) scale(1.07)}}

.dsp-amount{font-size:70px;font-weight:900;text-shadow:0 6px 24px rgba(0,0,0,.3);font-variant-numeric:tabular-nums;animation:fadeInUp .5s ease}
.dsp-qr{background:#fff;padding:16px;border-radius:18px;margin:16px 0;animation:qrPop .65s cubic-bezier(.34,1.56,.64,1);box-shadow:0 16px 40px rgba(0,0,0,.35)}
.dsp-code{font-size:16px;font-weight:700;opacity:.9;animation:fadeInUp .6s .2s ease both}
.dsp-wait{margin-top:14px;font-size:16px;font-weight:600;display:flex;align-items:center;gap:10px;opacity:.95;animation:fadeInUp .6s .35s ease both}

.dsp-check{font-size:76px;margin-bottom:6px;animation:cdPop .7s cubic-bezier(.34,1.56,.64,1)}
.dsp-thanks{font-size:34px;font-weight:900}
.dsp-payer{font-size:52px;font-weight:900;margin:10px 0;animation:namePop .9s .3s cubic-bezier(.34,1.56,.64,1) both;text-shadow:0 4px 20px rgba(0,0,0,.25);word-break:break-word}
.dsp-payer .at{opacity:.7}
.dsp-msg{font-size:19px;font-weight:600;opacity:.92}
.dsp-amt2{font-size:64px;font-weight:900;margin:16px 0 4px;font-variant-numeric:tabular-nums;animation:cdPop .8s .5s cubic-bezier(.34,1.56,.64,1) both;text-shadow:0 6px 24px rgba(0,0,0,.3)}
.dsp-time{font-size:17px;font-weight:600;opacity:.9}

.wl-indicator{position:fixed;top:14px;right:14px;background:rgba(0,0,0,.35);color:#fff;font-size:11px;font-weight:700;padding:6px 12px;border-radius:9999px;z-index:10040;pointer-events:none}
#fs-hint{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.55);color:#fff;font-size:12.5px;font-weight:700;padding:10px 20px;border-radius:9999px;z-index:10060;display:none}

@keyframes cdPop{0%{transform:scale(.4);opacity:0}60%{transform:scale(1.12)}100%{transform:scale(1);opacity:1}}

/* 🚨 নিরাপত্তা অ্যালার্ম */
.alarm-overlay{position:fixed;inset:0;background:#7f1d1d;z-index:20000;display:none;flex-direction:column;align-items:center;justify-content:center;color:#fff;text-align:center;padding:20px;animation:alarmBg .5s infinite}
.alarm-overlay.open{display:flex}
.alarm-icon{font-size:90px;animation:alarmShake .35s infinite}
.alarm-title{font-size:34px;font-weight:900;letter-spacing:2px}
.alarm-sub{font-size:16px;font-weight:600;margin:8px 0 20px;opacity:.9}
.alarm-pinbox{display:flex;gap:8px;margin-bottom:16px}
.alarm-pinbox input{width:52px;height:60px;text-align:center;font-size:28px;font-weight:900;border:none;border-radius:12px;background:rgba(255,255,255,.92);color:#7f1d1d;outline:none}
.alarm-pad{display:grid;grid-template-columns:repeat(3,76px);gap:10px}
.alarm-key{height:60px;border:none;border-radius:14px;background:rgba(255,255,255,.18);color:#fff;font-size:24px;font-weight:900;cursor:pointer;border:1px solid rgba(255,255,255,.3)}
.alarm-key:active{background:rgba(255,255,255,.4)}
.alarm-err{min-height:22px;font-size:14px;font-weight:800;margin-top:10px;color:#fecaca}
@keyframes alarmShake{0%,100%{transform:rotate(-8deg) scale(1)}50%{transform:rotate(8deg) scale(1.1)}}
@keyframes alarmBg{0%,100%{background:#7f1d1d}50%{background:#991b1b}}

.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:18px;display:flex;gap:14px;align-items:center;box-shadow:0 4px 6px -1px rgba(0,0,0,.05);transition:transform .25s,box-shadow .25s}
.stat-card:hover{transform:translateY(-4px);box-shadow:0 12px 24px -8px rgba(0,0,0,.15)}
.stat-icon{width:46px;height:46px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0}
.stat-val{font-size:21px;font-weight:800;font-variant-numeric:tabular-nums}
.stat-sub{font-size:12px;color:var(--muted);margin-top:2px}

.chart{display:flex;align-items:flex-end;gap:12px;height:150px;padding-top:8px}
.bar-col{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px;height:100%;justify-content:flex-end}
.bar{width:100%;max-width:44px;background:linear-gradient(180deg,#60a5fa,#2563eb);border-radius:8px 8px 3px 3px;animation:growBar .9s cubic-bezier(.34,1.56,.64,1)}
.bar-label{font-size:11px;color:var(--muted);font-weight:600}

.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.search-input{flex:1;min-width:180px;margin-bottom:0}
.filter-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.chip{border:1px solid var(--border);background:#f8fafc;border-radius:9999px;padding:7px 16px;font-size:12.5px;font-weight:700;cursor:pointer;color:var(--muted);transition:.2s}
.chip.active{background:var(--primary);color:#fff;border-color:var(--primary);box-shadow:0 4px 10px -2px rgba(37,99,235,.5)}

table{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:6px}
th,td{padding:11px 12px;text-align:left;border-bottom:1px solid var(--border)}
th{background:#f8fafc;color:var(--muted);font-weight:600;white-space:nowrap}
tbody tr{cursor:pointer;transition:.15s}
tbody tr:hover{background:#f8fafc}
.empty-row{text-align:center;color:var(--muted);padding:22px !important}
.code-tag{background:#e0f2fe;color:#0369a1;padding:3px 8px;border-radius:6px;font-family:monospace;font-weight:700;font-size:12.5px}
.st{padding:4px 10px;border-radius:9999px;font-size:11px;font-weight:800;white-space:nowrap;letter-spacing:.3px}
.st-success{background:#dcfce7;color:#15803d}
.st-cancelled{background:#fee2e2;color:#b91c1c}
.st-expired{background:#ffedd5;color:#c2410c}

.mini-list{display:flex;flex-direction:column;gap:8px;margin-top:6px}
.mini-item{display:flex;justify-content:space-between;align-items:center;background:#f8fafc;border:1px solid var(--border);border-radius:10px;padding:10px 14px;font-size:13.5px;animation:fadeInUp .4s ease}

#toast-wrap{position:fixed;bottom:22px;right:22px;display:flex;flex-direction:column;gap:10px;z-index:30000}
.toast{background:#0f172a;color:#fff;padding:13px 20px;border-radius:12px;font-size:14px;font-weight:600;box-shadow:0 10px 25px rgba(0,0,0,.3);transform:translateX(120%);opacity:0;transition:.4s cubic-bezier(.34,1.56,.64,1)}
.toast.show{transform:translateX(0);opacity:1}
.toast.success{background:var(--success)}
.toast.error{background:var(--danger)}

.modal{position:fixed;inset:0;background:rgba(15,23,42,.65);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;z-index:9998;padding:16px}
.modal.open{display:flex;animation:fadeInUp .3s ease}
.receipt{background:#fff;border-radius:16px;padding:26px;width:350px;max-width:100%;box-shadow:0 25px 60px rgba(0,0,0,.35)}
.r-head{text-align:center;font-size:19px;font-weight:800}
.r-sub{text-align:center;font-size:12px;color:var(--muted);margin-top:2px}
.r-sep{border-top:2px dashed var(--border);margin:14px 0}
.r-row{display:flex;justify-content:space-between;font-size:13.5px;margin:7px 0;gap:10px}
.r-total{display:flex;justify-content:space-between;font-size:18px;font-weight:800;margin:10px 0}
.modal-actions{display:flex;gap:8px;margin-top:16px;justify-content:center;flex-wrap:wrap}

.confetti{position:fixed;top:-12px;z-index:25000;pointer-events:none}
#dl-overlay{position:fixed;inset:0;background:rgba(15,23,42,.75);z-index:10001;display:none;align-items:center;justify-content:center;flex-direction:column;gap:14px;color:#fff}
#dl-overlay.open{display:flex}
.spinner{width:44px;height:44px;border:4px solid rgba(255,255,255,.25);border-top-color:#fff;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

@keyframes fadeInUp{from{opacity:0;transform:translateY(18px)}to{opacity:1;transform:translateY(0)}}
@keyframes gradientShift{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
@keyframes bounceDot{0%,80%,100%{transform:scale(.4);opacity:.5}40%{transform:scale(1);opacity:1}}
@keyframes qrPop{0%{transform:scale(.55);opacity:0}60%{transform:scale(1.06)}100%{transform:scale(1);opacity:1}}
@keyframes drawStroke{to{stroke-dashoffset:0}}
@keyframes floatY{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}
@keyframes growBar{from{height:0}}
@keyframes fall{to{transform:translateY(105vh) rotate(720deg)}}

@media print{body *{visibility:hidden}#receipt-print,#receipt-print *{visibility:visible}#receipt-print{position:fixed;top:0;left:50%;transform:translateX(-50%);box-shadow:none}}
</style>
</head>
<body>

<!-- 🖥️ কাস্টমার ডিসপ্লে -->
<div id="display-app">
  <div class="dsp-activate" id="dsp-activate">
    <div class="dsp-logo">⛽</div>
    <div class="dsp-name">{{ config.MERCHANT_NAME }}</div>
    <div style="font-size:14px;opacity:.7">কাস্টমার পেমেন্ট ডিসপ্লে</div>
    <div class="dsp-tap">👆 চালু করতে একবার টাচ করুন</div>
    <div style="font-size:12px;opacity:.6">স্ক্রিন সবসময় জ্বলজ্বল থাকবে</div>
  </div>

  <div class="dsp-screen idle" id="dsp-screen">
    <div class="dsp-merchant" id="dsp-merchant">{{ config.MERCHANT_NAME }}</div>

    <div id="dsp-idle">
      <div class="bubble-wrap">
        <div class="bubble b1"></div>
        <div class="bubble b2"></div>
        <div class="bubble b3"></div>
        <div class="bubble b4"></div>
        <div class="dsp-idle-logo">⛽</div>
      </div>
      <div class="dsp-idle-txt">🧾 বিল তৈরির অপেক্ষায়...</div>
      <div class="dsp-idle-sub">কাউন্টার থেকে কিউআর আসলে এখানে দেখা যাবে</div>
      <div class="dsp-idle-pulse"><span></span><span></span><span></span></div>
    </div>

    <div id="dsp-waiting" style="display:none">
      <div class="dsp-amount" id="dsp-amount">₹0</div>
      <div class="dsp-qr" id="dsp-qr"></div>
      <div class="dsp-code">কোড: <span id="dsp-code" class="code-tag"></span></div>
      <div class="dsp-wait"><span class="dots"><span></span><span></span><span></span></span> উপরের কিউআর স্ক্যান করে পেমেন্ট করুন</div>
    </div>

    <div id="dsp-success" style="display:none">
      <div class="dsp-check">✅</div>
      <div class="dsp-thanks">ধন্যবাদ!</div>
      <div class="dsp-payer"><span class="at">@</span><span id="dsp-payer">কাস্টমার</span></div>
      <div class="dsp-msg">আপনার পেমেন্ট সফল হয়েছে</div>
      <div class="dsp-amt2" id="dsp-amt2">₹0.00</div>
      <div class="dsp-time">🕐 <span id="dsp-time"></span></div>
    </div>
  </div>
  <div class="wl-indicator" id="dsp-wl">🔆 স্ক্রিন চালু</div>
  <div id="fs-hint">⛶ ফুল স্ক্রিনে ফিরতে স্ক্রিনে টাচ করুন</div>
</div>

<!-- ⛽ কাউন্টার -->
<div id="pos-app">
<div class="container">

  <div class="header-card">
    <div class="merchant-title">
      <div class="merchant-logo">⛽</div>
      <div>
        <h2 style="margin:0;font-size:20px;" id="hdr-merchant">{{ config.MERCHANT_NAME }}</h2>
        <div style="font-size:12.5px;opacity:.85;margin-top:2px;">অটো পেমেন্ট ভেরিফাই সিস্টেম</div>
      </div>
    </div>
    <div class="badge-group">
      <div class="badge badge-glass live-clock">🕐 <span id="clock">--:--:--</span></div>
      <div id="email-status-badge" class="badge badge-offline"><span class="live-dot" style="background:#f87171"></span> জিমেইল চেক হচ্ছে...</div>
    </div>
  </div>

  <div class="tabs">
    <button class="tab active" id="btn-pos" onclick="switchTab('pos')">⛽ বিল কাউন্টার</button>
    <button class="tab" id="btn-dash" onclick="switchTab('dash')">📊 ড্যাশবোর্ড</button>
  </div>

  <section id="tab-pos" class="tab-content active">
    <div class="grid-layout">
      <div class="card anim d1">
        <h3>⚙️ দোকান সেটিংস</h3>
        <label>ইউপিআই আইডি</label>
        <input type="text" id="upi-input" value="{{ config.UPI_ID }}">
        <label>দোকানের নাম</label>
        <input type="text" id="merchant-input" value="{{ config.MERCHANT_NAME }}">
        <label class="toggle-row">
          <input type="checkbox" id="strict-toggle" {{ 'checked' if config.STRICT_CODE else '' }}>
          🔐 কড়া মোড — ইমেইলে কোড থাকতেই হবে
        </label>
        <label class="toggle-row">
          <input type="checkbox" id="security-toggle" {{ 'checked' if config.SECURITY_SWITCH else '' }}>
          🚨 নিরাপত্তা সুইচ — চার্জার খুললে অ্যালার্ম বাজবে
        </label>
        <button class="btn dark" onclick="updateSettings()">💾 সেটিংস সেভ করুন</button>
        <button class="btn" style="background:#0d9488;margin-top:10px" onclick="window.open('/?mode=display','_blank')">🖥️ কাস্টমার ডিসপ্লে খুলুন (দ্বিতীয় ফোন)</button>
      </div>

      <div class="card qr-display anim d2">
        <h3 style="text-align:left">💳 পেমেন্ট কিউআর</h3>
        <div id="create-view">
          <div class="quick-row">
            <button class="quick-btn" onclick="setAmount(100)">₹100</button>
            <button class="quick-btn" onclick="setAmount(200)">₹200</button>
            <button class="quick-btn" onclick="setAmount(500)">₹500</button>
            <button class="quick-btn" onclick="setAmount(1000)">₹1000</button>
          </div>
          <input type="number" id="pay-amount" placeholder="টাকার পরিমাণ লিখুন" min="1" oninput="validateAmount()">
          <button class="btn" id="gen-btn" onclick="generateQR()" disabled>কিউআর কোড তৈরি করুন</button>
          <div id="lock-msg" style="color:var(--danger);font-size:13px;margin-top:10px;display:none">⚠️ জিমেইল কানেক্টেড নেই!</div>
        </div>

        <div id="payment-view" style="display:none">
          <div id="pay-active">
            <div id="order-info" style="font-weight:800;color:var(--primary);font-size:18px"></div>
            <div style="font-size:13px;color:var(--muted);margin-top:4px">পেমেন্ট কোড: <span id="secret-code-display" class="code-tag"></span></div>
            <div id="qr-box"></div>
            <div id="payment-status" class="status-pill status-waiting"><span class="dots"><span></span><span></span><span></span></span> ব্যাংক কনফার্মেশনের অপেক্ষায়...</div>
            <div class="timer-wrap"><div id="timer-bar"></div></div>
            <div id="timer-text">মেয়াদ শেষ হবে 05:00</div>
            <button class="btn red" onclick="cancelOrder()">✖ বিল বাতিল করুন</button>
          </div>

          <div id="pay-success" style="display:none">
            <svg class="checkmark" viewBox="0 0 52 52"><circle cx="26" cy="26" r="25"/><path d="M14.1 27.2l7.1 7.2 16.7-16.8"/></svg>

            <div class="thank-card">
              <div class="thank-title">🙏 পেমেন্ট পাওয়া গেছে</div>
              <div class="thank-name"><span class="at">@</span><span id="thank-name">কাস্টমার</span></div>
              <div class="thank-msg">পেমেন্টের জন্য ধন্যবাদ</div>
            </div>

            <div class="thank-amount-box">
              <div class="thank-label">💰 প্রাপ্ত টাকা</div>
              <div class="thank-amount" id="thank-amount">₹0.00</div>
              <div class="thank-time">🕐 সময়: <span id="thank-time">--:--:--</span></div>
            </div>

            <div id="success-details"></div>

            <button class="btn" onclick="showReceipt(lastRecord)">🧾 রশিদ দেখুন / ডাউনলোড করুন</button>
            <button class="btn gray" onclick="resetQRView()">➕ নতুন বিল</button>
          </div>
        </div>
      </div>
    </div>

    <div class="card anim d3">
      <h3>🧾 সাম্প্রতিক পেমেন্ট</h3>
      <div class="mini-list" id="recent-list"><div style="color:var(--muted);font-size:13.5px;text-align:center;padding:14px">এখনো কোনো পেমেন্ট হয়নি</div></div>
    </div>
  </section>

  <section id="tab-dash" class="tab-content">
    <div class="stats-grid">
      <div class="stat-card"><div class="stat-icon" style="background:#eff6ff">💰</div><div><div class="stat-val" id="stat-today">₹0.00</div><div class="stat-sub" id="stat-today-count">আজকের বিক্রি — 0 টি</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:#f0fdf4">📈</div><div><div class="stat-val" id="stat-total">₹0.00</div><div class="stat-sub">মোট আয় (সব মিলিয়ে)</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:#faf5ff">🧮</div><div><div class="stat-val" id="stat-avg">₹0.00</div><div class="stat-sub">গড় বিক্রি</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:#fef2f2">🚫</div><div><div class="stat-val" id="stat-cancelled">0</div><div class="stat-sub" id="stat-cancelled-sub">বাতিল / মেয়াদোত্তীর্ণ</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:#ecfeff">🎯</div><div><div class="stat-val" id="stat-rate">0%</div><div class="stat-sub">সফলতার হার</div></div></div>
    </div>

    <div class="card anim d2">
      <h3>📊 সাম্প্রতিক সফল লেনদেন</h3>
      <div class="chart" id="chart"><div style="color:var(--muted);font-size:13px">এখনো ডেটা নেই</div></div>
    </div>

    <div class="card anim d3">
      <div class="toolbar">
        <input class="search-input" id="search-input" placeholder="🔍 খুঁজুন: অর্ডার / কোড / ইউটিআর / নাম..." oninput="renderTable()">
        <button class="btn small" style="background:var(--success)" onclick="window.location='/api/export'">⬇️ সিএসভি ডাউনলোড</button>
        <button class="btn small" style="background:#334155" onclick="refreshDashboard()">🔄 রিফ্রেশ</button>
      </div>
      <div class="filter-row">
        <button class="chip active" onclick="setFilter('ALL',this)">📋 সব</button>
        <button class="chip" onclick="setFilter('SUCCESS',this)">✅ সফল</button>
        <button class="chip" onclick="setFilter('CANCELLED',this)">❌ বাতিল</button>
        <button class="chip" onclick="setFilter('EXPIRED',this)">⏰ মেয়াদ শেষ</button>
      </div>
      <div style="overflow-x:auto">
      <table>
        <thead><tr><th>অর্ডার</th><th>কোড</th><th>টাকা</th><th>অবস্থা</th><th>নাম</th><th>ইউটিআর</th><th>তারিখ ও সময়</th></tr></thead>
        <tbody id="history-body"><tr><td colspan="7" class="empty-row">এখনো কোনো লেনদেন নেই</td></tr></tbody>
      </table>
      </div>
    </div>
  </section>
</div>
</div>

<!-- 🚨 নিরাপত্তা অ্যালার্ম -->
<div class="alarm-overlay" id="alarm-overlay">
  <div class="alarm-icon">🚨</div>
  <div class="alarm-title">নিরাপত্তা অ্যালার্ম!</div>
  <div class="alarm-sub">ডিভাইস চার্জার থেকে খোলা হয়েছে! বন্ধ করতে পিন দিন।</div>
  <div class="alarm-pinbox" id="alarm-pins"></div>
  <div class="alarm-pad">
    <button class="alarm-key" onclick="pinKey('1')">১</button>
    <button class="alarm-key" onclick="pinKey('2')">২</button>
    <button class="alarm-key" onclick="pinKey('3')">৩</button>
    <button class="alarm-key" onclick="pinKey('4')">৪</button>
    <button class="alarm-key" onclick="pinKey('5')">৫</button>
    <button class="alarm-key" onclick="pinKey('6')">৬</button>
    <button class="alarm-key" onclick="pinKey('7')">৭</button>
    <button class="alarm-key" onclick="pinKey('8')">৮</button>
    <button class="alarm-key" onclick="pinKey('9')">৯</button>
    <button class="alarm-key" onclick="pinKey('C')">C</button>
    <button class="alarm-key" onclick="pinKey('0')">০</button>
    <button class="alarm-key" style="background:#16a34a" onclick="pinSubmit()">✓</button>
  </div>
  <div class="alarm-err" id="alarm-err"></div>
</div>

<div class="modal" id="receipt-modal" onclick="if(event.target===this)closeReceipt()">
  <div>
    <div class="receipt" id="receipt-print">
      <div class="r-head">⛽ <span id="r-merchant"></span></div>
      <div class="r-sub">ইউপিআই পেমেন্ট রশিদ</div>
      <div class="r-sub" style="font-size:11px;">📧 tanmaykundu439@gmail.com</div>
      <div class="r-sep"></div>
      <div class="r-row"><span>অর্ডার আইডি</span><b id="r-order"></b></div>
      <div class="r-row"><span>পেমেন্ট কোড</span><b id="r-code" class="code-tag"></b></div>
      <div class="r-row"><span>পাঠানোর নাম</span><b id="r-payer"></b></div>
      <div class="r-row"><span>ইউটিআর / রেফ</span><b id="r-utr"></b></div>
      <div class="r-row"><span>তারিখ</span><b id="r-date"></b></div>
      <div class="r-row"><span>সময়</span><b id="r-time"></b></div>
      <div class="r-sep"></div>
      <div class="r-total"><span>মোট পরিশোধ</span><span id="r-amount"></span></div>
      <div class="r-sep"></div>
      <div class="r-sub">✅ জিমেইল অটো-ভেরিফাই সিস্টেমে যাচাই হয়েছে</div>
      <div class="r-sub">ধন্যবাদ! আবার আসবেন 🙏</div>
    </div>
    <div class="modal-actions">
      <button class="btn small" style="background:var(--success)" onclick="downloadReceipt('png')">⬇️ পিএনজি ডাউনলোড</button>
      <button class="btn small orange" onclick="downloadReceipt('pdf')">⬇️ পিডিএফ ডাউনলোড</button>
      <button class="btn small dark" onclick="window.print()">🖨️ প্রিন্ট</button>
      <button class="btn small" style="background:#64748b" onclick="closeReceipt()">বন্ধ করুন</button>
    </div>
  </div>
</div>

<div id="dl-overlay">
  <div class="spinner"></div>
  <div id="dl-text" style="font-weight:700;font-size:15px">রশিদ তৈরি হচ্ছে...</div>
</div>

<div id="toast-wrap"></div>

<script>
/* ================= মোড নির্ণয় + শেয়ার্ড ইউটিল ================= */
const IS_DISPLAY = new URLSearchParams(location.search).get('mode') === 'display';
const FS_SUPPORTED = !!(document.documentElement.requestFullscreen || document.documentElement.webkitRequestFullscreen);
const el = id => document.getElementById(id);

if(IS_DISPLAY){ document.body.classList.add('display-mode'); }

/* 🔆 Wake Lock */
let wakeLock=null;
async function requestWakeLock(){
  try{
    if('wakeLock' in navigator){
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release',()=>{ setTimeout(requestWakeLock,1000); });
    }
  }catch(e){}
}
document.addEventListener('visibilitychange',()=>{
  if(document.visibilityState==='visible') requestWakeLock();
});

/* 🔊 অডিও */
let audioCtx=null;
function getAudio(){
  try{
    if(!audioCtx) audioCtx = new (window.AudioContext||window.webkitAudioContext)();
    if(audioCtx.state==='suspended') audioCtx.resume();
    return audioCtx;
  }catch(e){ return null; }
}
function playDing(){
  const ctx=getAudio(); if(!ctx) return;
  [523.25,659.25,783.99,1046.5].forEach((f,i)=>{
    const o=ctx.createOscillator(),g=ctx.createGain();
    o.type='sine'; o.frequency.value=f;
    const t0=ctx.currentTime+i*0.12;
    g.gain.setValueAtTime(0.25,t0);
    g.gain.exponentialRampToValueAtTime(0.01,t0+0.35);
    o.connect(g); g.connect(ctx.destination);
    o.start(t0); o.stop(t0+0.4);
  });
}
function playPop(){
  const ctx=getAudio(); if(!ctx) return;
  const o=ctx.createOscillator(),g=ctx.createGain();
  o.type='triangle';
  const t=ctx.currentTime;
  o.frequency.setValueAtTime(300,t);
  o.frequency.exponentialRampToValueAtTime(900,t+0.18);
  g.gain.setValueAtTime(0.3,t);
  g.gain.exponentialRampToValueAtTime(0.01,t+0.25);
  o.connect(g); g.connect(ctx.destination);
  o.start(t); o.stop(t+0.3);
}
function launchConfetti(){
  const colors=['#2563eb','#16a34a','#f59e0b','#ef4444','#8b5cf6','#06b6d4'];
  for(let i=0;i<70;i++){
    const c=document.createElement('div');
    c.className='confetti';
    const size=6+Math.random()*8;
    c.style.cssText=`left:${Math.random()*100}vw;width:${size}px;height:${size*(Math.random()>.5?1:.5)}px;background:${colors[i%colors.length]};border-radius:${Math.random()>.5?'50%':'2px'};animation:fall ${2+Math.random()*1.5}s linear forwards`;
    document.body.appendChild(c);
    setTimeout(()=>c.remove(),4000);
  }
}
function countUp(elm,target){
  const t0=performance.now(), dur=1000;
  function step(t){
    const p=Math.min((t-t0)/dur,1), e=1-Math.pow(1-p,3);
    elm.textContent='₹'+(target*e).toFixed(2);
    if(p<1)requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

/* 🚨 নিরাপত্তা — চার্জার মনিটর */
let battery=null, armedCharging=null, securityOn=false, lastServerAck=0, alarmActive=false, pinBuffer='';
async function initBattery(){
  try{
    if(navigator.getBattery){
      battery = await navigator.getBattery();
      battery.addEventListener('chargingchange', secCheck);
    }
  }catch(e){}
}
async function secCheck(){
  try{
    const d = await fetch('/api/security').then(r=>r.json());
    securityOn = !!d.security;
    if(d.ack > lastServerAck && alarmActive){ lastServerAck=d.ack; stopAlarm(); }
    if(!securityOn && alarmActive){ stopAlarm(); armedCharging=null; return; }

    if(!battery) return;
    if(securityOn){
      if(armedCharging===null) armedCharging = battery.charging;
      if(armedCharging===true && !battery.charging && !alarmActive){
        triggerAlarm();
      }
      if(battery.charging) armedCharging=true;
    }else{
      armedCharging=null;
    }
  }catch(e){}
}
function startSiren(){
  const ctx=getAudio(); if(!ctx) return;
  window._alarmOsc=ctx.createOscillator(); window._alarmGain=ctx.createGain();
  window._alarmOsc.type='sawtooth';
  window._alarmGain.gain.value=1.0;
  window._alarmOsc.connect(window._alarmGain); window._alarmGain.connect(ctx.destination);
  window._alarmOsc.start();
  let up=true;
  window._alarmLoop=setInterval(()=>{
    const t=ctx.currentTime;
    window._alarmOsc.frequency.cancelScheduledValues(t);
    window._alarmOsc.frequency.setValueAtTime(up?600:1300,t);
    window._alarmOsc.frequency.linearRampToValueAtTime(up?1300:600,t+0.5);
    up=!up;
  },500);
  if(navigator.vibrate){
    window._vibLoop=setInterval(()=>navigator.vibrate([600,200,600,200,600]),2000);
    navigator.vibrate([600,200,600]);
  }
}
function stopSiren(){
  try{ if(window._alarmOsc){window._alarmOsc.stop(); window._alarmOsc.disconnect();} if(window._alarmGain)window._alarmGain.disconnect(); }catch(e){}
  if(window._alarmLoop){clearInterval(window._alarmLoop); window._alarmLoop=null;}
  if(window._vibLoop){clearInterval(window._vibLoop); navigator.vibrate(0); window._vibLoop=null;}
  window._alarmOsc=window._alarmGain=null;
}
function triggerAlarm(){
  alarmActive=true;
  startSiren();
  el('alarm-overlay').classList.add('open');
  pinBuffer=''; renderPins(); el('alarm-err').innerText='';
}
function stopAlarm(){
  alarmActive=false;
  stopSiren();
  el('alarm-overlay').classList.remove('open');
}
function renderPins(){
  const box=el('alarm-pins');
  box.innerHTML='';
  for(let i=0;i<4;i++){
    const inp=document.createElement('input');
    inp.type='password'; inp.readOnly=true;
    inp.value = pinBuffer[i] ? '●' : '';
    box.appendChild(inp);
  }
}
function pinKey(k){
  if(k==='C'){ pinBuffer=''; }
  else if(pinBuffer.length<4){ pinBuffer+=k; }
  renderPins();
}
async function pinSubmit(){
  try{
    const r = await fetch('/api/alarm_stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:pinBuffer})});
    const d = await r.json();
    if(d.status==='ok'){ stopAlarm(); lastServerAck=d.ack; }
    else{ el('alarm-err').innerText='❌ ভুল পিন! আবার চেষ্টা করুন'; pinBuffer=''; renderPins(); }
  }catch(e){ el('alarm-err').innerText='নেটওয়ার্ক সমস্যা'; }
}
setInterval(secCheck, 2000);
initBattery();

/* ================= 🖥️ ডিসপ্লে মোড ================= */
let dspActivated=false;
async function goFullscreen(){
  try{
    const docEl=document.documentElement;
    if(docEl.requestFullscreen){ await docEl.requestFullscreen({navigationUI:'hide'}); }
    else if(docEl.webkitRequestFullscreen){ await docEl.webkitRequestFullscreen(); }
    if(screen.orientation && screen.orientation.lock){
      screen.orientation.lock('portrait').catch(()=>{});
    }
  }catch(e){}
}

if(IS_DISPLAY){
  el('dsp-activate').addEventListener('pointerdown', function(){
    if(dspActivated) return;
    dspActivated=true;
    el('dsp-activate').classList.add('hide');
    getAudio();
    requestWakeLock();
    goFullscreen();
  });

  document.addEventListener('touchstart', e=>{
    if(!dspActivated) return;
    if(alarmActive) return;
    e.preventDefault();
    if(FS_SUPPORTED && !document.fullscreenElement){
      goFullscreen();
    }
  }, {passive:false});
  document.addEventListener('touchmove', e=>{
    if(dspActivated && !alarmActive) e.preventDefault();
  }, {passive:false});
  document.addEventListener('dblclick', e=>{ if(dspActivated) e.preventDefault(); });
  document.addEventListener('contextmenu', e=>e.preventDefault());

  document.addEventListener('fullscreenchange', ()=>{
    const hint=el('fs-hint');
    if(FS_SUPPORTED && dspActivated && !document.fullscreenElement && !alarmActive){
      hint.style.display='block';
      setTimeout(()=>{ hint.style.display='none'; }, 6000);
    }else{
      hint.style.display='none';
    }
  });

  /* ডিসপ্লে স্টেট পোল */
  let prevOrderId=null;
  function pollDisplay(){
    fetch('/api/display_state').then(r=>r.json()).then(st=>{
      el('dsp-merchant').innerText = st.merchant || 'বিএন এন্টারপ্রাইজেস';
      const scr=el('dsp-screen');
      el('dsp-idle').style.display = st.mode==='idle'?'block':'none';
      el('dsp-waiting').style.display = st.mode==='waiting'?'block':'none';
      el('dsp-success').style.display = st.mode==='success'?'block':'none';
      scr.className='dsp-screen '+(st.mode==='idle'?'idle':'');

      if(st.mode==='waiting'){
        el('dsp-code').innerText=st.secret_code;
        if(prevOrderId!==st.order_id){
          el('dsp-qr').innerHTML='';
          prevOrderId=st.order_id;
          new QRCode(el('dsp-qr'),{text:st.upi_url,width:230,height:230,correctLevel:QRCode.CorrectLevel.M});
          countUp(el('dsp-amount'), Number(st.amount));
          playPop();
          launchConfetti();
        }
      }

      if(st.mode==='success' && prevOrderId!=='S'+st.order_id){
        prevOrderId='S'+st.order_id;
        el('dsp-payer').innerText = st.payer || 'কাস্টমার';
        el('dsp-time').innerText = st.time || '';
        countUp(el('dsp-amt2'), Number(st.amount));
        playDing();
        launchConfetti();
      }
      if(st.mode==='idle'){ prevOrderId=null; }
    }).catch(()=>{});
  }
  setInterval(pollDisplay, 1200);
  pollDisplay();
}

/* ================= ⛽ কাউন্টার মোড ================= */
let pollTimer=null, timerInt=null, timeLeft=300, lastRecord=null, historyData=[], gmailOK=false, currentOrderId=null, statusFilter='ALL', currentReceiptRec=null;

if(!IS_DISPLAY){
  function updateClock(){ el('clock').innerText = new Date().toLocaleTimeString('en-IN',{hour12:true}); }
  setInterval(updateClock,1000); updateClock();

  function switchTab(name){
    document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(s=>s.classList.remove('active'));
    el('btn-'+name).classList.add('active');
    el('tab-'+name).classList.add('active');
    if(name==='dash') refreshDashboard();
  }
  function refreshDashboard(){ loadStats(); loadHistory(); }

  function showToast(msg,type='success'){
    const t=document.createElement('div');
    t.className='toast '+type; t.innerText=msg;
    el('toast-wrap').appendChild(t);
    setTimeout(()=>t.classList.add('show'),20);
    setTimeout(()=>{t.classList.remove('show'); setTimeout(()=>t.remove(),400);},3200);
  }

  function checkEmailStatus(){
    fetch('/api/check_email_status').then(r=>r.json()).then(d=>{
      gmailOK = d.connected;
      const badge=el('email-status-badge'), lock=el('lock-msg');
      if(gmailOK){
        badge.className='badge badge-online';
        badge.innerHTML='<span class="live-dot"></span> জিমেইল কানেক্টেড';
        lock.style.display='none';
      }else{
        badge.className='badge badge-offline';
        badge.innerHTML='<span class="live-dot" style="background:#f87171"></span> জিমেইল কানেক্টেড নেই';
        lock.style.display='block'; lock.innerText='⚠️ '+d.message;
      }
      validateAmount();
    }).catch(()=>{ gmailOK=false; validateAmount(); });
  }
  setInterval(checkEmailStatus,20000);

  function syncSecurityToggle(){
    fetch('/api/security').then(r=>r.json()).then(d=>{
      el('security-toggle').checked = !!d.security;
    }).catch(()=>{});
  }
  setInterval(syncSecurityToggle, 5000);

  function updateSettings(){
    fetch('/api/update_settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        upi_id:el('upi-input').value,
        merchant_name:el('merchant-input').value,
        strict:el('strict-toggle').checked,
        security:el('security-toggle').checked
      })})
    .then(r=>r.json()).then(d=>{
      el('hdr-merchant').innerText=el('merchant-input').value;
      showToast(d.message,'success');
    }).catch(()=>showToast('সেভ করা যায়নি!','error'));
  }

  function setAmount(v){ el('pay-amount').value=v; validateAmount(); }
  function validateAmount(){
    const v=parseFloat(el('pay-amount').value);
    el('gen-btn').disabled = !(v>0) || !gmailOK;
  }
  function generateQR(){
    const amt=parseFloat(el('pay-amount').value);
    if(!amt||amt<=0) return showToast('সঠিক টাকার পরিমাণ লিখুন!','error');
    fetch(`/api/create_order?amount=${amt}`)
      .then(r=>r.json()).then(d=>{
        if(d.status==='error'){ showToast(d.message,'error'); return; }
        currentOrderId = d.order_id;
        el('create-view').style.display='none';
        el('payment-view').style.display='block';
        el('pay-active').style.display='block';
        el('pay-success').style.display='none';
        el('order-info').innerText=`মোট: ₹${Number(d.amount).toFixed(2)}`;
        el('secret-code-display').innerText=d.secret_code;
        el('qr-box').innerHTML='';
        el('payment-status').className='status-pill status-waiting';
        el('payment-status').innerHTML='<span class="dots"><span></span><span></span><span></span></span> ব্যাংক কনফার্মেশনের অপেক্ষায়...';
        new QRCode(el('qr-box'),{text:d.upi_url,width:200,height:200,correctLevel:QRCode.CorrectLevel.M});
        startPolling(d.order_id);
      }).catch(()=>showToast('বিল তৈরি করা যায়নি!','error'));
  }

  function startPolling(orderId){
    stopPolling();
    timeLeft=300; updateTimer();
    timerInt=setInterval(()=>{ timeLeft--; updateTimer(); if(timeLeft<=0){ stopPolling(); showExpired(); } },1000);
    pollTimer=setInterval(()=>{
      fetch(`/api/verify_order?order_id=${orderId}`).then(r=>r.json()).then(d=>{
        if(d.status==='success') onVerified(d.record);
        else if(d.status==='expired'){ stopPolling(); showExpired(); }
        else if(d.status==='cancelled'){ stopPolling(); resetQRView(); }
      }).catch(()=>{});
    },1500);
  }
  function stopPolling(){ if(pollTimer)clearInterval(pollTimer); if(timerInt)clearInterval(timerInt); pollTimer=timerInt=null; }
  function updateTimer(){
    const bar=el('timer-bar');
    bar.style.width=(timeLeft/300*100)+'%';
    bar.classList.toggle('low',timeLeft<60);
    const m=Math.floor(timeLeft/60), s=timeLeft%60;
    el('timer-text').innerText=`মেয়াদ শেষ হবে ${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
  }
  function showExpired(){
    const st=el('payment-status');
    st.className='status-pill status-expired';
    st.innerText='⏰ বিলের মেয়াদ শেষ! ড্যাশবোর্ডে দেখা যাবে।';
  }

  function cancelOrder(){
    if(!currentOrderId){ resetQRView(); return; }
    fetch(`/api/cancel_order?order_id=${currentOrderId}`)
      .then(r=>r.json()).then(d=>{
        stopPolling();
        if(d.status==='success'){ showToast('❌ বিল বাতিল হয়েছে — ড্যাশবোর্ডে দেখা যাবে','error'); }
        resetQRView();
      }).catch(()=>showToast('বাতিল করা যায়নি!','error'));
  }

  function onVerified(rec){
    stopPolling(); lastRecord=rec;
    el('pay-active').style.display='none';
    el('pay-success').style.display='block';

    const payer = rec.payer || 'কাস্টমার';
    const amt = Number(rec.amount);

    el('thank-name').innerText = payer;
    countUp(el('thank-amount'), amt);
    el('thank-time').innerText = rec.time || new Date().toLocaleTimeString('en-IN',{hour12:true});

    el('success-details').innerHTML=`
      <div style="font-size:13px;color:var(--muted);margin-bottom:10px;">ইউটিআর: <span class="code-tag">${rec.utr}</span></div>`;

    playDing(); launchConfetti(); blinkTitle();
    loadStats(); loadHistory();
  }

  function blinkTitle(){
    let n=0;
    const t=setInterval(()=>{
      document.title = n%2===0 ? '✅ পেমেন্ট পাওয়া গেছে!' : 'বিএন এন্টারপ্রাইজেস';
      if(++n>6){ clearInterval(t); document.title='বিএন এন্টারপ্রাইজেস'; }
    },600);
  }
  function resetQRView(){
    stopPolling(); currentOrderId=null;
    fetch('/api/clear_current').catch(()=>{});   /* 🆕 ডিসপ্লে আবার বাবলে ফিরবে */
    el('payment-view').style.display='none';
    el('create-view').style.display='block';
    el('pay-amount').value='';
    validateAmount();
  }

  function loadHistory(){
    fetch('/api/history').then(r=>r.json()).then(list=>{
      historyData=list;
      renderTable(); renderRecent(); renderChart();
    });
  }
  function statusPill(s){
    if(s==='SUCCESS') return '<span class="st st-success">✅ সফল</span>';
    if(s==='CANCELLED') return '<span class="st st-cancelled">❌ বাতিল</span>';
    if(s==='EXPIRED') return '<span class="st st-expired">⏰ মেয়াদ শেষ</span>';
    return '<span class="st">'+s+'</span>';
  }
  function setFilter(f,btn){ statusFilter=f; document.querySelectorAll('.chip').forEach(c=>c.classList.remove('active')); btn.classList.add('active'); renderTable(); }

  function renderTable(){
    const q=(el('search-input').value||'').toLowerCase();
    const rows=historyData.filter(t=>{
      if(statusFilter!=='ALL' && (t.status||'SUCCESS')!==statusFilter) return false;
      if(!q) return true;
      return (t.order_id||'').toLowerCase().includes(q) || (t.secret_code||'').toLowerCase().includes(q)
        || String(t.utr||'').toLowerCase().includes(q) || String(t.amount).includes(q)
        || (t.payer||'').toLowerCase().includes(q);
    });
    const tb=el('history-body');
    if(!rows.length){ tb.innerHTML='<tr><td colspan="7" class="empty-row">'+(q?'কিছু পাওয়া যায়নি':'এখনো কোনো লেনদেন নেই')+'</td></tr>'; return; }
    tb.innerHTML=[...rows].reverse().map(t=>{
      const idx=historyData.indexOf(t);
      return `<tr onclick='showReceipt(historyData[${idx}])' title="রশিদ দেখতে ক্লিক করুন">
        <td><b>${t.order_id}</b></td>
        <td><span class="code-tag">${t.secret_code}</span></td>
        <td>₹${Number(t.amount).toFixed(2)}</td>
        <td>${statusPill(t.status||'SUCCESS')}</td>
        <td>${t.payer? '<b>'+t.payer+'</b>' : '<span style="color:var(--muted)">—</span>'}</td>
        <td>${(t.status||'SUCCESS')==='SUCCESS'? t.utr : '—'}</td>
        <td>${t.date||''} · ${t.time}</td>
      </tr>`;
    }).join('');
  }
  function renderRecent(){
    const box=el('recent-list');
    const last=[...historyData].slice(-4).reverse();
    if(!last.length){ box.innerHTML='<div style="color:var(--muted);font-size:13.5px;text-align:center;padding:14px">এখনো কোনো পেমেন্ট হয়নি</div>'; return; }
    box.innerHTML=last.map(t=>`
      <div class="mini-item">
        <div><span class="code-tag">${t.secret_code}</span> <b style="margin-left:8px">₹${Number(t.amount).toFixed(2)}</b></div>
        <div style="text-align:right">
          ${t.payer? '<div style="font-weight:700;font-size:12.5px">👤 '+t.payer+'</div>':''}
          <div style="color:var(--muted);font-size:12px">${(t.status||'SUCCESS')==='SUCCESS'?'✅':t.status==='CANCELLED'?'❌':'⏰'} ${t.time}</div>
        </div>
      </div>`).join('');
  }
  function renderChart(){
    const chart=el('chart');
    const success=historyData.filter(t=>(t.status||'SUCCESS')==='SUCCESS');
    const last=success.slice(-8);
    if(!last.length){ chart.innerHTML='<div style="color:var(--muted);font-size:13px">এখনো ডেটা নেই</div>'; return; }
    const max=Math.max(...last.map(t=>Number(t.amount)),1);
    chart.innerHTML=last.map(t=>{
      const v=Number(t.amount);
      return `<div class="bar-col">
        <div class="bar" style="height:${Math.max(v/max*100,4).toFixed(0)}%"></div>
        <span class="bar-label">₹${v>=1000?(v/1000).toFixed(1)+'k':v.toFixed(0)}</span>
      </div>`;
    }).join('');
  }
  function animateNum(elm,target,prefix='₹',suffix=''){
    const start=parseFloat(elm.dataset.v||0), t0=performance.now(), dur=700;
    function step(t){
      const p=Math.min((t-t0)/dur,1), e=1-Math.pow(1-p,3);
      elm.textContent=prefix+(start+(target-start)*e).toFixed(2)+suffix;
      if(p<1)requestAnimationFrame(step); else elm.dataset.v=target;
    }
    requestAnimationFrame(step);
  }
  function loadStats(){
    fetch('/api/stats').then(r=>r.json()).then(d=>{
      animateNum(el('stat-today'),d.today_sales);
      animateNum(el('stat-total'),d.total_sales);
      animateNum(el('stat-avg'),d.avg_sale);
      animateNum(el('stat-cancelled'),d.cancelled_expired,'');
      animateNum(el('stat-rate'),d.success_rate,'','%' );
      el('stat-today-count').innerText=`আজকের বিক্রি — ${d.today_count} টি`;
      el('stat-cancelled-sub').innerText=`বাতিল ${d.cancelled_count} · মেয়াদ শেষ ${d.expired_count}`;
    });
  }

  function showReceipt(rec){
    if(!rec) return;
    currentReceiptRec = rec;
    el('r-merchant').innerText=el('merchant-input').value;
    el('r-order').innerText=rec.order_id;
    el('r-code').innerText=rec.secret_code;
    el('r-payer').innerText=rec.payer||'ইউপিআই কাস্টমার';
    el('r-utr').innerText=(rec.status||'SUCCESS')==='SUCCESS'? rec.utr : '—';
    el('r-date').innerText=rec.date||'—';
    el('r-time').innerText=rec.time;
    el('r-amount').innerText='₹'+Number(rec.amount).toFixed(2);
    el('receipt-modal').classList.add('open');
  }
  function closeReceipt(){ el('receipt-modal').classList.remove('open'); }

  async function downloadReceipt(type){
    if(!currentReceiptRec) return showToast('আগে একটা রশিদ খুলুন!','error');
    const node = el('receipt-print');
    const rec = currentReceiptRec;
    const fname = `Rashid_${rec.order_id}_${rec.secret_code}`;
    const overlay = el('dl-overlay'), dlText = el('dl-text');

    overlay.classList.add('open');
    dlText.innerText = type==='pdf' ? 'পিডিএফ তৈরি হচ্ছে...' : 'ছবি তৈরি হচ্ছে...';

    try{
      const modal = el('receipt-modal');
      modal.style.visibility = 'hidden';
      const canvas = await html2canvas(node, {scale:3, backgroundColor:'#ffffff', useCORS:true});
      modal.style.visibility = 'visible';

      if(type === 'png'){
        const a = document.createElement('a');
        a.href = canvas.toDataURL('image/png');
        a.download = fname + '.png';
        document.body.appendChild(a); a.click(); a.remove();
        showToast('✅ পিএনজি ডাউনলোড হয়েছে!','success');
      } else {
        const { jsPDF } = window.jspdf;
        const pdf = new jsPDF('p','mm','a5');
        const pageW = pdf.internal.pageSize.getWidth();
        const pageH = pdf.internal.pageSize.getHeight();
        const margin = 10;
        const imgW = pageW - margin*2;
        const imgH = canvas.height * imgW / canvas.width;
        const finalH = Math.min(imgH, pageH - margin*2);
        pdf.addImage(canvas.toDataURL('image/jpeg',0.95),'JPEG',margin,margin,imgW,finalH);
        pdf.save(fname + '.pdf');
        showToast('✅ পিডিএফ ডাউনলোড হয়েছে!','success');
      }
    }catch(e){
      console.error(e);
      el('receipt-modal').style.visibility='visible';
      showToast('❌ ডাউনলোড করা যায়নি!','error');
    }finally{
      overlay.classList.remove('open');
    }
  }

  checkEmailStatus(); loadHistory(); loadStats(); validateAmount(); syncSecurityToggle();
  setInterval(()=>{ if(el('tab-dash').classList.contains('active')) loadStats(); },10000);
  document.addEventListener('pointerdown', function once(){ requestWakeLock(); document.removeEventListener('pointerdown',once); });
}
</script>
</body>
</html>
"""

# ================= API রাউটসমূহ =================
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, config=CONFIG)

@app.route('/api/check_email_status')
def api_check_email_status():
    ok, msg = test_gmail_connection()
    return jsonify({"connected": ok, "message": msg})

@app.route('/api/update_settings', methods=['POST'])
def api_update_settings():
    data = request.json or {}
    if "upi_id" in data and data["upi_id"].strip():
        CONFIG["UPI_ID"] = data["upi_id"].strip()
    if "merchant_name" in data and data["merchant_name"].strip():
        CONFIG["MERCHANT_NAME"] = data["merchant_name"].strip()
    if "strict" in data:
        CONFIG["STRICT_CODE"] = bool(data["strict"])
    if "security" in data:
        CONFIG["SECURITY_SWITCH"] = bool(data["security"])
    return jsonify({"status": "success", "message": "✅ সেটিংস সেভ হয়েছে!"})

# 🚨 নিরাপত্তা
@app.route('/api/security')
def api_security():
    return jsonify({"security": CONFIG["SECURITY_SWITCH"], "ack": ALARM_ACK})

@app.route('/api/alarm_stop', methods=['POST'])
def api_alarm_stop():
    global ALARM_ACK
    data = request.json or {}
    pin = str(data.get("pin", ""))
    if pin == str(CONFIG["SECURITY_PIN"]):
        CONFIG["SECURITY_SWITCH"] = False
        ALARM_ACK += 1
        return jsonify({"status": "ok", "ack": ALARM_ACK})
    return jsonify({"status": "error", "message": "ভুল পিন"}), 401

# 🖥️ ডিসপ্লে স্টেট — 💾 ফাইল থেকে পড়ে (cross-device sync)
@app.route('/api/display_state')
def api_display_state():
    cur, orders, uu, ue = read_orders()
    changed, recs = cleanup_orders_dict(orders)
    if changed:
        write_orders(cur, orders, uu, ue)
        append_history(recs)

    st = {
        "mode": "idle",
        "merchant": CONFIG["MERCHANT_NAME"],
        "order_id": None, "amount": 0, "secret_code": "", "upi_url": "",
        "payer": None, "utr": "", "time": ""
    }
    o = orders.get(cur) if cur else None
    if o:
        st["order_id"] = cur
        st["amount"] = float(o["amount"])
        st["secret_code"] = o["secret_code"]
        if o["status"] == "PENDING":
            st["mode"] = "waiting"
            st["upi_url"] = (f"upi://pay?pa={quote(CONFIG['UPI_ID'])}"
                             f"&pn={quote(CONFIG['MERCHANT_NAME'])}"
                             f"&am={o['amount']}&cu=INR&tn={o['secret_code']}&tr={cur}")
        elif o["status"] == "SUCCESS":
            # ২০ সেকেন্ড পর অটো বাবলে ফেরা
            if time.time() - o.get("success_at", 0) < SUCCESS_SHOW_SEC:
                rec = o.get("record") or {}
                st["mode"] = "success"
                st["payer"] = rec.get("payer")
                st["utr"] = rec.get("utr", "")
                st["time"] = rec.get("time", "")
            else:
                st["mode"] = "idle"
    return jsonify(st)

# 💾 "নতুন বিল" — ডিসপ্লে রিসেট
@app.route('/api/clear_current')
def api_clear_current():
    cur, orders, uu, ue = read_orders()
    if cur is not None:
        write_orders(None, orders, uu, ue)
    return jsonify({"status": "ok"})

@app.route('/api/create_order')
def api_create_order():
    ok, msg = test_gmail_connection()
    if not ok:
        return jsonify({"status": "error", "message": msg}), 400

    try:
        amt = round(float(request.args.get("amount", 0)), 2)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "ভুল টাকার পরিমাণ"}), 400
    if amt < 1 or amt > 100000:
        return jsonify({"status": "error", "message": "টাকা ₹1 – ₹1,00,000 এর মধ্যে হতে হবে"}), 400

    cur, orders, uu, ue = read_orders()
    changed, recs = cleanup_orders_dict(orders)
    if changed:
        append_history(recs)

    pending_codes = {o["secret_code"] for o in orders.values() if o.get("status") == "PENDING"}
    while True:
        code = "FP" + str(random.randint(100000, 999999))
        if code not in pending_codes:
            break

    order_id = "ORD" + uuid.uuid4().hex[:6].upper()
    upi_url = (f"upi://pay?pa={quote(CONFIG['UPI_ID'])}"
               f"&pn={quote(CONFIG['MERCHANT_NAME'])}"
               f"&am={amt}&cu=INR&tn={code}&tr={order_id}")

    orders[order_id] = {
        "amount": amt,
        "secret_code": code,
        "status": "PENDING",
        "created_at": time.time()
    }
    write_orders(order_id, orders, uu, ue)   # 💾 ফাইলে সেভ — সব device দেখবে
    return jsonify({
        "order_id": order_id,
        "secret_code": code,
        "amount": amt,
        "upi_url": upi_url
    })

@app.route('/api/verify_order')
def api_verify_order():
    order_id = request.args.get('order_id', '')
    cur, orders, uu, ue = read_orders()
    order = orders.get(order_id)
    if not order:
        return jsonify({"status": "error", "message": "অর্ডার পাওয়া যায়নি"}), 404

    if order["status"] == "SUCCESS":
        return jsonify({"status": "success", "record": order.get("record")})

    if order["status"] == "CANCELLED":
        return jsonify({"status": "cancelled"})

    changed, recs = cleanup_orders_dict(orders)
    if changed:
        write_orders(cur, orders, uu, ue)
        append_history(recs)
    if order["status"] == "EXPIRED":
        return jsonify({"status": "expired"})

    strict = CONFIG.get("STRICT_CODE", False)
    verified, utr, payer, reason = verify_payment_mail(
        order["secret_code"], order["amount"], order["created_at"], strict, ue)

    if verified and utr not in uu:
        now = datetime.datetime.now()
        record = {
            "order_id": order_id,
            "secret_code": order["secret_code"],
            "amount": float(order["amount"]),
            "status": "SUCCESS",
            "payer": payer,
            "utr": utr,
            "time": now.strftime("%I:%M:%S %p"),
            "date": now.strftime("%Y-%m-%d")
        }
        order["status"] = "SUCCESS"
        order["record"] = record
        order["success_at"] = time.time()
        uu.add(utr)
        write_orders(cur, orders, uu, ue)   # 💾 সেভ — ডিসপ্লে সাথে সাথে দেখবে
        append_history([record])
        return jsonify({"status": "success", "record": record})

    if not verified:
        _log_debug(order_id, reason)

    return jsonify({"status": "waiting"})

@app.route('/api/cancel_order')
def api_cancel_order():
    order_id = request.args.get('order_id', '')
    cur, orders, uu, ue = read_orders()
    order = orders.get(order_id)
    if not order:
        return jsonify({"status": "error", "message": "অর্ডার পাওয়া যায়নি"}), 404

    if order["status"] == "SUCCESS":
        return jsonify({"status": "error", "message": "পেমেন্ট হয়ে গেছে! বাতিল করা যাবে না।"}), 400
    if order["status"] != "PENDING":
        return jsonify({"status": "error", "message": "অর্ডার ইতিমধ্যে বাতিল/মেয়াদ শেষ"}), 400

    order["status"] = "CANCELLED"
    now = datetime.datetime.now()
    record = {
        "order_id": order_id,
        "secret_code": order["secret_code"],
        "amount": float(order["amount"]),
        "status": "CANCELLED",
        "payer": None,
        "utr": "—",
        "time": now.strftime("%I:%M:%S %p"),
        "date": now.strftime("%Y-%m-%d")
    }
    order["record"] = record
    write_orders(cur if cur != order_id else None, orders, uu, ue)
    append_history([record])
    return jsonify({"status": "success", "message": "বিল বাতিল হয়েছে"})

@app.route('/api/history')
def api_history():
    return jsonify(get_history())

@app.route('/api/stats')
def api_stats():
    hist = get_history()
    today = datetime.date.today().strftime("%Y-%m-%d")
    success_tx = [t for t in hist if t.get("status") == "SUCCESS"]
    today_tx = [t for t in success_tx if t.get("date") == today]
    today_sum = sum(float(t["amount"]) for t in today_tx)
    total_sum = sum(float(t["amount"]) for t in success_tx)
    cancelled_count = len([t for t in hist if t.get("status") == "CANCELLED"])
    expired_count = len([t for t in hist if t.get("status") == "EXPIRED"])
    total_orders = len(hist)
    return jsonify({
        "today_sales": round(today_sum, 2),
        "today_count": len(today_tx),
        "total_sales": round(total_sum, 2),
        "total_count": len(success_tx),
        "avg_sale": round(total_sum / len(success_tx), 2) if success_tx else 0,
        "cancelled_count": cancelled_count,
        "expired_count": expired_count,
        "cancelled_expired": cancelled_count + expired_count,
        "success_rate": round(len(success_tx) / total_orders * 100, 1) if total_orders else 0
    })

@app.route('/api/export')
def api_export():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["অর্ডার আইডি", "কোড", "টাকা", "অবস্থা", "নাম", "ইউটিআর", "তারিখ", "সময়"])
    for t in get_history():
        writer.writerow([t["order_id"], t["secret_code"], t["amount"], t.get("status", "SUCCESS"),
                         t.get("payer", ""), t.get("utr", ""), t.get("date", ""), t["time"]])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=transactions.csv"}
    )

# ================= ডিবাগ =================
@app.route('/api/debug/emails')
def api_debug_emails():
    emails = get_recent_emails()
    result = []
    for msg, subject, body in emails[:10]:
        result.append({
            "subject": subject[:80],
            "from": str(msg.get("From", ""))[:60],
            "date": str(msg.get("Date", ""))[:30],
            "extracted_payer": extract_payer_name(subject, body),
            "body_preview": body[:300]
        })
    return jsonify(result)

@app.route('/api/debug/log')
def api_debug_log():
    return jsonify(DEBUG_LOG)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
