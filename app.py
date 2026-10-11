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
    "STRICT_CODE": False,       # 🔓 False = Relaxed (কোড না থাকলেও amount+time মিললে verify)
    "SECURITY_SWITCH": False,   # 🚨 Anti-theft charger alarm
    "SECURITY_PIN": "1234"
}

PAYMENT_HISTORY = []
ACTIVE_ORDERS = {}
USED_UTRS = set()
USED_EMAIL_IDS = set()
DEBUG_LOG = []
CURRENT_ORDER_ID = None      # 🆕 Display ডিভাইসের জন্য latest order
ALARM_ACK = 0                # 🆕 Alarm stop counter

ORDER_TIMEOUT = 300
EMAIL_FETCH_COUNT = 15
EMAIL_LOOKBACK_MIN = 30
HISTORY_FILE = "payment_history.json"

_gmail_lock = threading.Lock()
_imap = None                 # 🆕 সবসময় alive কানেকশন (speed-এর মূল চাবি)
_email_cache = []
_last_fetch = 0

def _log_debug(order_id, reason):
    DEBUG_LOG.append({"time": datetime.datetime.now().strftime("%H:%M:%S"),
                      "order_id": order_id, "reason": reason})
    del DEBUG_LOG[:-30]

# ================= হিস্টোরি সেভ / লোড =================
def save_history():
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(PAYMENT_HISTORY, f)
    except Exception:
        pass

def load_history():
    global PAYMENT_HISTORY
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE) as f:
                PAYMENT_HISTORY = json.load(f)
            for t in PAYMENT_HISTORY:
                t.setdefault("status", "SUCCESS")
                t.setdefault("payer", None)
                t.setdefault("date", "")
                t.setdefault("manual", False)
    except Exception:
        pass

load_history()

# ================= ⚡ Persistent Gmail কানেকশন (Speed Boost) =================
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

# ================= ইমেইল বডি পার্সার =================
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

# ================= 🆕 Background Email Poller (২ সেকেন্ড পরপর) =================
def _fetch_emails_once():
    global _email_cache, _last_fetch, _imap
    try:
        with _gmail_lock:
            m = _get_imap()
            m.select("inbox")
            since = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
            _, messages = m.search(None, f'(SINCE "{since}")')
            ids = messages[0].split()[-EMAIL_FETCH_COUNT:]
            parsed = []
            for m_id in reversed(ids):
                _, data = m.fetch(m_id, "(RFC822)")
                for part in data:
                    if isinstance(part, tuple):
                        msg = email.message_from_bytes(part[1])
                        parsed.append((msg, _decode_subject(msg), _parse_body(msg)))
            _email_cache = parsed
            _last_fetch = time.time()
            return True
    except Exception:
        try:
            if _imap:
                _imap.logout()
        except Exception:
            pass
        _imap = None
        return False

def email_poller():
    # প্রথমে সাথে সাথে একবার, তারপর প্রতি ২ সেকেন্ডে
    while True:
        ok = _fetch_emails_once()
        time.sleep(2 if ok else 3)

threading.Thread(target=email_poller, daemon=True).start()

def get_recent_emails():
    return _email_cache

def test_gmail_connection():
    if _last_fetch and (time.time() - _last_fetch) < 25:
        return True, "Gmail Connected & Active"
    # কখনো fetch হয়নি হলে sync একবার চেষ্টা
    if _fetch_emails_once():
        return True, "Gmail Connected & Active"
    return False, "Gmail Connection Error — কিছুক্ষণ পরে আবার দেখুন"

# ================= Payer Name Extractor (FamPay + সব ব্যাংক) =================
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
        return -120 < age < EMAIL_LOOKBACK_MIN * 60
    except Exception:
        return True

# ================= পেমেন্ট ভেরিফিকেশন (ক্যাশ থেকে — instant) =================
def verify_payment_mail(secret_code, expected_amount, created_at=None, strict=True):
    emails = get_recent_emails()
    target = str(secret_code).strip().lower()
    expected = round(float(expected_amount), 2)

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
        if mid in USED_EMAIL_IDS:
            last_reason = "email_used"
            continue

        if created_at is not None:
            try:
                dt = parsedate_to_datetime(msg.get("Date"))
                if dt and dt.timestamp() < (created_at - 120):
                    last_reason = "too_old"
                    continue
            except Exception:
                pass

        text = f"{subject} {body}"
        low = text.lower()

        if "debited" in low and "credited" not in low and "received" not in low:
            continue

        if not any(w in low for w in ["credited", "received", "deposited", "added", "upi"]):
            continue

        code_found = target in low

        amounts = re.findall(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d{1,2})?)", text, re.IGNORECASE)
        amount_match = False
        for a in amounts:
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
        USED_EMAIL_IDS.add(mid)
        if len(USED_EMAIL_IDS) > 200:
            USED_EMAIL_IDS.clear()
        return True, utr, payer, "matched"

    return False, None, None, last_reason

# ================= Expired অর্ডার → History =================
def cleanup_expired_orders():
    now = time.time()
    changed = False
    for oid, o in list(ACTIVE_ORDERS.items()):
        if o["status"] == "PENDING" and now - o["created_at"] > ORDER_TIMEOUT:
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
            PAYMENT_HISTORY.append(rec)
            changed = True
    if changed:
        save_history()

# ================= Merchant UI =================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>BN Enterprises - Fuel POS Terminal</title>
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

/* ================= 🖥️ Customer Display Device Mode ================= */
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
.dsp-idle-logo{font-size:60px;animation:floatY 3s ease-in-out infinite}
.dsp-idle-txt{font-size:22px;font-weight:700;opacity:.85;margin-top:12px}

.dsp-amount{font-size:70px;font-weight:900;text-shadow:0 6px 24px rgba(0,0,0,.3);font-variant-numeric:tabular-nums}
.dsp-qr{background:#fff;padding:16px;border-radius:18px;margin:18px 0;animation:qrPop .6s cubic-bezier(.34,1.56,.64,1);box-shadow:0 16px 40px rgba(0,0,0,.3)}
.dsp-code{font-size:16px;font-weight:700;opacity:.9}
.dsp-wait{margin-top:14px;font-size:16px;font-weight:600;display:flex;align-items:center;gap:10px;opacity:.95}

.dsp-check{font-size:76px;margin-bottom:6px;animation:cdPop .7s cubic-bezier(.34,1.56,.64,1)}
.dsp-thanks{font-size:34px;font-weight:900}
.dsp-payer{font-size:52px;font-weight:900;margin:10px 0;animation:namePop .9s .3s cubic-bezier(.34,1.56,.64,1) both;text-shadow:0 4px 20px rgba(0,0,0,.25);word-break:break-word}
.dsp-payer .at{opacity:.7}
.dsp-msg{font-size:19px;font-weight:600;opacity:.92}
.dsp-amt2{font-size:64px;font-weight:900;margin:16px 0 4px;font-variant-numeric:tabular-nums;animation:cdPop .8s .5s cubic-bezier(.34,1.56,.64,1) both;text-shadow:0 6px 24px rgba(0,0,0,.3)}
.dsp-time{font-size:17px;font-weight:600;opacity:.9}
.dsp-merchant{position:fixed;top:20px;left:0;right:0;text-align:center;font-size:15px;font-weight:800;opacity:.8;letter-spacing:1px;pointer-events:none}

.wl-indicator{position:fixed;top:14px;right:14px;background:rgba(0,0,0,.35);color:#fff;font-size:11px;font-weight:700;padding:6px 12px;border-radius:9999px;z-index:10040;pointer-events:none}

@keyframes cdPop{0%{transform:scale(.4);opacity:0}60%{transform:scale(1.12)}100%{transform:scale(1);opacity:1}}

/* ================= 🚨 Security Alarm ================= */
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

<!-- ============================================================ -->
<!-- ================== 🖥️ CUSTOMER DISPLAY APP ================= -->
<!-- ============================================================ -->
<div id="display-app">
  <div class="dsp-activate" id="dsp-activate">
    <div class="dsp-logo">⛽</div>
    <div class="dsp-name">{{ config.MERCHANT_NAME }}</div>
    <div style="font-size:14px;opacity:.7">Customer Payment Display</div>
    <div class="dsp-tap">👆 চালু করতে একবার টাচ করুন</div>
    <div style="font-size:12px;opacity:.6">স্ক্রিন সবসময় জ্বলজ্বল থাকবে · টাচ লক সক্রিয়</div>
  </div>

  <div class="dsp-screen idle" id="dsp-screen">
    <div class="dsp-merchant" id="dsp-merchant">{{ config.MERCHANT_NAME }}</div>

    <div id="dsp-idle">
      <div class="dsp-idle-logo">⛽</div>
      <div class="dsp-idle-txt">নতুন বিলের অপেক্ষায়...<br><span style="font-size:14px;opacity:.6">Waiting for new bill</span></div>
    </div>

    <div id="dsp-waiting" style="display:none">
      <div class="dsp-amount" id="dsp-amount">₹0</div>
      <div class="dsp-qr" id="dsp-qr"></div>
      <div class="dsp-code">Code: <span id="dsp-code" class="code-tag"></span></div>
      <div class="dsp-wait"><span class="dots"><span></span><span></span><span></span></span> উপরের QR স্ক্যান করে পেমেন্ট করুন</div>
    </div>

    <div id="dsp-success" style="display:none">
      <div class="dsp-check">✅</div>
      <div class="dsp-thanks">Thank You!</div>
      <div class="dsp-payer"><span class="at">@</span><span id="dsp-payer">Customer</span></div>
      <div class="dsp-msg">আপনার পেমেন্ট সফল হয়েছে</div>
      <div class="dsp-amt2" id="dsp-amt2">₹0.00</div>
      <div class="dsp-time">🕐 <span id="dsp-time"></span></div>
    </div>
  </div>
  <div class="wl-indicator" id="dsp-wl">🔆 Screen Awake</div>
</div>

<!-- ============================================================ -->
<!-- ===================== ⛽ POS APP ============================ -->
<!-- ============================================================ -->
<div id="pos-app">
<div class="container">

  <div class="header-card">
    <div class="merchant-title">
      <div class="merchant-logo">⛽</div>
      <div>
        <h2 style="margin:0;font-size:20px;" id="hdr-merchant">{{ config.MERCHANT_NAME }}</h2>
        <div style="font-size:12.5px;opacity:.85;margin-top:2px;">UPI Auto-Verify Engine v4.0 · Dual Device</div>
      </div>
    </div>
    <div class="badge-group">
      <div class="badge badge-glass live-clock">🕐 <span id="clock">--:--:--</span></div>
      <div id="email-status-badge" class="badge badge-offline"><span class="live-dot" style="background:#f87171"></span> Checking Gmail...</div>
    </div>
  </div>

  <div class="tabs">
    <button class="tab active" id="btn-pos" onclick="switchTab('pos')">⛽ POS Terminal</button>
    <button class="tab" id="btn-dash" onclick="switchTab('dash')">📊 Merchant Dashboard</button>
  </div>

  <section id="tab-pos" class="tab-content active">
    <div class="grid-layout">
      <div class="card anim d1">
        <h3>⚙️ Merchant Settings</h3>
        <label>Active UPI ID</label>
        <input type="text" id="upi-input" value="{{ config.UPI_ID }}">
        <label>Merchant / Pump Name</label>
        <input type="text" id="merchant-input" value="{{ config.MERCHANT_NAME }}">
        <label class="toggle-row">
          <input type="checkbox" id="strict-toggle" {{ 'checked' if config.STRICT_CODE else '' }}>
          🔐 Strict Mode — ইমেইলে FP কোড থাকতেই হবে
        </label>
        <label class="toggle-row">
          <input type="checkbox" id="security-toggle" {{ 'checked' if config.SECURITY_SWITCH else '' }}>
          🚨 Security Switch — চার্জার খুললে অ্যালার্ম (PIN: লাগবে বন্ধ করতে)
        </label>
        <button class="btn dark" onclick="updateSettings()">💾 Save Settings</button>
        <button class="btn" style="background:#0d9488;margin-top:10px" onclick="window.open('/?mode=display','_blank')">🖥️ Open Customer Display (2nd Device)</button>
        <div style="margin-top:14px;background:#f8fafc;border:1px solid var(--border);border-radius:10px;padding:12px;font-size:12.5px;color:var(--muted);line-height:1.6">
          ℹ️ <b>দুই ডিভাইস সেটআপ:</b> এই ডিভাইস থেকে QR তৈরি করুন → কাস্টমারের সামনে রাখা ফোনে <b>?mode=display</b> লিংক খুলুন। ওখানে QR + Success অটো দেখাবে, touch লক থাকবে।<br>
          🚨 <b>Security ON:</b> সাইট খোলা যেকোনো ডিভাইস চার্জার থেকে খুললে সাইরেন বাজবে। বন্ধ করতে ডিভাইসে PIN (<b id="pin-hint">****</b>) দিন বা এখানে switch OFF করুন।
        </div>
      </div>

      <div class="card qr-display anim d2">
        <h3 style="text-align:left">💳 Dynamic Payment QR</h3>
        <div id="create-view">
          <div class="quick-row">
            <button class="quick-btn" onclick="setAmount(100)">₹100</button>
            <button class="quick-btn" onclick="setAmount(200)">₹200</button>
            <button class="quick-btn" onclick="setAmount(500)">₹500</button>
            <button class="quick-btn" onclick="setAmount(1000)">₹1000</button>
          </div>
          <input type="number" id="pay-amount" placeholder="টাকার পরিমাণ লিখুন" min="1" oninput="validateAmount()">
          <button class="btn" id="gen-btn" onclick="generateQR()" disabled>Generate QR Code</button>
          <div id="lock-msg" style="color:var(--danger);font-size:13px;margin-top:10px;display:none">⚠️ Gmail disconnected!</div>
        </div>

        <div id="payment-view" style="display:none">
          <div id="pay-active">
            <div id="order-info" style="font-weight:800;color:var(--primary);font-size:18px"></div>
            <div style="font-size:13px;color:var(--muted);margin-top:4px">UPI Note Code: <span id="secret-code-display" class="code-tag"></span></div>
            <div id="qr-box"></div>
            <div id="payment-status" class="status-pill status-waiting"><span class="dots"><span></span><span></span><span></span></span> ব্যাংক কনফার্মেশনের অপেক্ষায়...</div>
            <div id="verify-hint" style="font-size:12px;color:var(--muted);margin-top:8px;min-height:16px;text-align:center"></div>
            <div class="timer-wrap"><div id="timer-bar"></div></div>
            <div id="timer-text">Expires in 05:00</div>
            <button class="btn red" onclick="cancelOrder()">✖ Cancel Order</button>
          </div>

          <div id="pay-success" style="display:none">
            <svg class="checkmark" viewBox="0 0 52 52"><circle cx="26" cy="26" r="25"/><path d="M14.1 27.2l7.1 7.2 16.7-16.8"/></svg>

            <div class="thank-card">
              <div class="thank-title">🙏 PAYMENT RECEIVED</div>
              <div class="thank-name"><span class="at">@</span><span id="thank-name">Customer</span></div>
              <div class="thank-msg">Thank you for your payment · পেমেন্টের জন্য ধন্যবাদ</div>
            </div>

            <div class="thank-amount-box">
              <div class="thank-label">💰 AMOUNT RECEIVED</div>
              <div class="thank-amount" id="thank-amount">₹0.00</div>
              <div class="thank-time">🕐 Received at: <span id="thank-time">--:--:--</span></div>
            </div>

            <div id="success-details"></div>

            <button class="btn" onclick="showReceipt(lastRecord)">🧾 View / Download Receipt</button>
            <button class="btn gray" onclick="resetQRView()">➕ New Bill</button>
          </div>
        </div>
      </div>
    </div>

    <div class="card anim d3">
      <h3>🧾 Recent Payments</h3>
      <div class="mini-list" id="recent-list"><div style="color:var(--muted);font-size:13.5px;text-align:center;padding:14px">No verified transactions yet</div></div>
    </div>
  </section>

  <section id="tab-dash" class="tab-content">
    <div class="stats-grid">
      <div class="stat-card"><div class="stat-icon" style="background:#eff6ff">💰</div><div><div class="stat-val" id="stat-today">₹0.00</div><div class="stat-sub" id="stat-today-count">Today's Sales — 0 txn</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:#f0fdf4">📈</div><div><div class="stat-val" id="stat-total">₹0.00</div><div class="stat-sub">Total Revenue (All Time)</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:#faf5ff">🧮</div><div><div class="stat-val" id="stat-avg">₹0.00</div><div class="stat-sub">Average Sale Value</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:#fef2f2">🚫</div><div><div class="stat-val" id="stat-cancelled">0</div><div class="stat-sub" id="stat-cancelled-sub">Cancelled / Expired</div></div></div>
      <div class="stat-card"><div class="stat-icon" style="background:#ecfeff">🎯</div><div><div class="stat-val" id="stat-rate">0%</div><div class="stat-sub">Success Rate</div></div></div>
    </div>

    <div class="card anim d2">
      <h3>📊 Last Successful Transactions</h3>
      <div class="chart" id="chart"><div style="color:var(--muted);font-size:13px">No data yet</div></div>
    </div>

    <div class="card anim d3">
      <div class="toolbar">
        <input class="search-input" id="search-input" placeholder="🔍 Search Order / Code / UTR / Payer..." oninput="renderTable()">
        <button class="btn small" style="background:var(--success)" onclick="window.location='/api/export'">⬇️ Export CSV</button>
        <button class="btn small" style="background:#334155" onclick="refreshDashboard()">🔄 Refresh</button>
      </div>
      <div class="filter-row">
        <button class="chip active" onclick="setFilter('ALL',this)">📋 All</button>
        <button class="chip" onclick="setFilter('SUCCESS',this)">✅ Success</button>
        <button class="chip" onclick="setFilter('CANCELLED',this)">❌ Cancelled</button>
        <button class="chip" onclick="setFilter('EXPIRED',this)">⏰ Expired</button>
      </div>
      <div style="overflow-x:auto">
      <table>
        <thead><tr><th>Order ID</th><th>Code</th><th>Amount</th><th>Status</th><th>Payer</th><th>UTR / Ref</th><th>Date & Time</th></tr></thead>
        <tbody id="history-body"><tr><td colspan="7" class="empty-row">No transactions yet</td></tr></tbody>
      </table>
      </div>
    </div>
  </section>
</div>
</div>

<!-- 🚨 Security Alarm Overlay -->
<div class="alarm-overlay" id="alarm-overlay">
  <div class="alarm-icon">🚨</div>
  <div class="alarm-title">SECURITY ALARM!</div>
  <div class="alarm-sub">ডিভাইস চার্জার থেকে খোলা হয়েছে! বন্ধ করতে PIN দিন।</div>
  <div class="alarm-pinbox" id="alarm-pins"></div>
  <div class="alarm-pad">
    <button class="alarm-key" onclick="pinKey('1')">1</button>
    <button class="alarm-key" onclick="pinKey('2')">2</button>
    <button class="alarm-key" onclick="pinKey('3')">3</button>
    <button class="alarm-key" onclick="pinKey('4')">4</button>
    <button class="alarm-key" onclick="pinKey('5')">5</button>
    <button class="alarm-key" onclick="pinKey('6')">6</button>
    <button class="alarm-key" onclick="pinKey('7')">7</button>
    <button class="alarm-key" onclick="pinKey('8')">8</button>
    <button class="alarm-key" onclick="pinKey('9')">9</button>
    <button class="alarm-key" onclick="pinKey('C')">C</button>
    <button class="alarm-key" onclick="pinKey('0')">0</button>
    <button class="alarm-key" style="background:#16a34a" onclick="pinSubmit()">✓</button>
  </div>
  <div class="alarm-err" id="alarm-err"></div>
</div>

<div class="modal" id="receipt-modal" onclick="if(event.target===this)closeReceipt()">
  <div>
    <div class="receipt" id="receipt-print">
      <div class="r-head">⛽ <span id="r-merchant"></span></div>
      <div class="r-sub">UPI Payment Receipt</div>
      <div class="r-sub" style="font-size:11px;">📧 tanmaykundu439@gmail.com</div>
      <div class="r-sep"></div>
      <div class="r-row"><span>Order ID</span><b id="r-order"></b></div>
      <div class="r-row"><span>UPI Code</span><b id="r-code" class="code-tag"></b></div>
      <div class="r-row"><span>Payer Name</span><b id="r-payer"></b></div>
      <div class="r-row"><span>UTR / Ref</span><b id="r-utr"></b></div>
      <div class="r-row"><span>Date</span><b id="r-date"></b></div>
      <div class="r-row"><span>Time</span><b id="r-time"></b></div>
      <div class="r-sep"></div>
      <div class="r-total"><span>Total Paid</span><span id="r-amount"></span></div>
      <div class="r-sep"></div>
      <div class="r-sub">✅ Verified via Gmail Auto-Engine</div>
      <div class="r-sub">ধন্যবাদ! আবার আসবেন 🙏</div>
    </div>
    <div class="modal-actions">
      <button class="btn small" style="background:var(--success)" onclick="downloadReceipt('png')">⬇️ Download PNG</button>
      <button class="btn small orange" onclick="downloadReceipt('pdf')">⬇️ Download PDF</button>
      <button class="btn small dark" onclick="window.print()">🖨️ Print</button>
      <button class="btn small" style="background:#64748b" onclick="closeReceipt()">Close</button>
    </div>
  </div>
</div>

<div id="dl-overlay">
  <div class="spinner"></div>
  <div id="dl-text" style="font-weight:700;font-size:15px">Receipt তৈরি হচ্ছে...</div>
</div>

<div id="toast-wrap"></div>

<script>
/* ================================================================
   MODE DETECT — ?mode=display হলে Customer Display ডিভাইস
================================================================ */
const IS_DISPLAY = new URLSearchParams(location.search).get('mode') === 'display';
const el = id => document.getElementById(id);

if(IS_DISPLAY){
  document.body.classList.add('display-mode');
}

/* ================================================================
   🔆 WAKE LOCK — স্ক্রিন কখনো নিভবে না (দুই মোডেই)
================================================================ */
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

/* ================================================================
   🔊 Audio Engine (success ding + alarm siren)
================================================================ */
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
/* 🚨 Siren — ফুল ভলিউম, লুপ */
let alarmOsc=null, alarmGain=null, alarmLoop=null, vibLoop=null, alarmActive=false;
function startSiren(){
  const ctx=getAudio(); if(!ctx) return;
  alarmOsc=ctx.createOscillator(); alarmGain=ctx.createGain();
  alarmOsc.type='sawtooth';
  alarmGain.gain.value=1.0;   // ফুল ভলিউম
  alarmOsc.connect(alarmGain); alarmGain.connect(ctx.destination);
  alarmOsc.start();
  let up=true;
  alarmLoop=setInterval(()=>{
    const t=ctx.currentTime;
    alarmOsc.frequency.cancelScheduledValues(t);
    alarmOsc.frequency.setValueAtTime(up?600:1300,t);
    alarmOsc.frequency.linearRampToValueAtTime(up?1300:600,t+0.5);
    up=!up;
  },500);
  // ভাইব্রেশন
  if(navigator.vibrate){
    vibLoop=setInterval(()=>navigator.vibrate([600,200,600,200,600]),2000);
    navigator.vibrate([600,200,600]);
  }
}
function stopSiren(){
  try{ if(alarmOsc){alarmOsc.stop(); alarmOsc.disconnect();} if(alarmGain)alarmGain.disconnect(); }catch(e){}
  if(alarmLoop){clearInterval(alarmLoop); alarmLoop=null;}
  if(vibLoop){clearInterval(vibLoop); navigator.vibrate(0); vibLoop=null;}
  alarmOsc=alarmGain=null;
}

/* ================================================================
   🚨 SECURITY — চার্জার মনিটর (Battery API + সার্ভার switch)
================================================================ */
let battery=null, armedCharging=null, securityOn=false, lastServerAck=0, pinBuffer='';
async function initBattery(){
  try{
    if(navigator.getBattery){
      battery = await navigator.getBattery();
      battery.addEventListener('chargingchange', secCheck);
    }else{
      const wl=el('dsp-wl'); if(wl && IS_DISPLAY) wl.innerText='⚠️ Security: Chrome/Android লাগবে';
    }
  }catch(e){}
}
async function secCheck(){
  try{
    const d = await fetch('/api/security').then(r=>r.json());
    const wasOn = securityOn;
    securityOn = !!d.security;
    // সার্ভার থেকে alarm-stop ack এলে বন্ধ
    if(d.ack > lastServerAck && alarmActive){ lastServerAck=d.ack; stopAlarm(); }
    if(!securityOn && alarmActive){ stopAlarm(); armedCharging=null; return; }

    if(!battery) return;
    if(securityOn){
      if(armedCharging===null) armedCharging = battery.charging;  // baseline
      if(armedCharging===true && !battery.charging && !alarmActive){
        triggerAlarm();
      }
      if(battery.charging) armedCharging=true;
    }else{
      armedCharging=null;
    }
  }catch(e){}
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
    else{ el('alarm-err').innerText='❌ ভুল PIN! আবার চেষ্টা করুন'; pinBuffer=''; renderPins(); }
  }catch(e){ el('alarm-err').innerText='Network error'; }
}
setInterval(secCheck, 2000);
initBattery();

/* ================================================================
   Display Mode — Touch পুরোপুরি লক
================================================================ */
if(IS_DISPLAY){
  document.addEventListener('touchstart', e=>{
    if(!alarmActive) e.preventDefault();
  }, {passive:false});
  document.addEventListener('touchmove', e=>e.preventDefault(), {passive:false});
  document.addEventListener('dblclick', e=>e.preventDefault());
  document.addEventListener('contextmenu', e=>e.preventDefault());

  // একবার টাচ → activate (wake lock + audio unlock) → এরপর লক
  el('dsp-activate').addEventListener('click', function once(){
    el('dsp-activate').classList.add('hide');
    getAudio();
    requestWakeLock();
    secCheck();
  });
}

/* ================================================================
   Display Mode — Server state poll
================================================================ */
let lastSuccessShown=null;
function dspCountUp(elm,target){
  const t0=performance.now(), dur=1000;
  function step(t){
    const p=Math.min((t-t0)/dur,1), e=1-Math.pow(1-p,3);
    elm.textContent='₹'+(target*e).toFixed(2);
    if(p<1)requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}
function pollDisplay(){
  fetch('/api/display_state').then(r=>r.json()).then(st=>{
    el('dsp-merchant').innerText = st.merchant || 'BN Enterprises';
    const scr=el('dsp-screen');
    el('dsp-idle').style.display = st.mode==='idle'?'block':'none';
    el('dsp-waiting').style.display = st.mode==='waiting'?'block':'none';
    el('dsp-success').style.display = st.mode==='success'?'block':'none';

    scr.className='dsp-screen '+(st.mode==='idle'?'idle':'');

    if(st.mode==='waiting'){
      el('dsp-amount').innerText='₹'+Number(st.amount).toFixed(st.amount%1?2:0);
      el('dsp-code').innerText=st.secret_code;
      if(el('dsp-qr').dataset.oid!==st.order_id){
        el('dsp-qr').innerHTML='';
        el('dsp-qr').dataset.oid=st.order_id;
        new QRCode(el('dsp-qr'),{text:st.upi_url,width:230,height:230,correctLevel:QRCode.CorrectLevel.M});
      }
    }

    if(st.mode==='success' && lastSuccessShown!==st.order_id){
      lastSuccessShown=st.order_id;
      el('dsp-payer').innerText = st.payer || 'Customer';
      el('dsp-time').innerText = st.time || '';
      dspCountUp(el('dsp-amt2'), Number(st.amount));
      playDing(); launchConfetti();
    }
  }).catch(()=>{});
}
if(IS_DISPLAY){
  setInterval(pollDisplay, 1500);
  pollDisplay();
}

/* ================================================================
   POS MODE — সব লজিক
================================================================ */
let pollTimer=null, timerInt=null, timeLeft=300, lastRecord=null, historyData=[], gmailOK=false, currentOrderId=null, statusFilter='ALL', currentReceiptRec=null;

const REASONS={
  no_emails:'কোনো নতুন ইমেইল পাওয়া যায়নি — Gmail-এ FamPay/ব্যাংকের মেইল এসেছে কিনা দেখুন',
  no_code_match:'ইমেইলে FP কোড পাওয়া যায়নি — Settings-এ Strict Mode বন্ধ করুন',
  no_amount_match:'অ্যামাউন্ট ম্যাচ হয়নি এখনো',
  email_used:'ইমেইল অন্য অর্ডারে ব্যবহৃত হয়েছে',
  too_old:'ইমেইলটি অর্ডারের আগের — বাদ',
  no_match:'অপেক্ষা চলছে...'
};

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
        badge.innerHTML='<span class="live-dot"></span> Gmail Connected';
        lock.style.display='none';
      }else{
        badge.className='badge badge-offline';
        badge.innerHTML='<span class="live-dot" style="background:#f87171"></span> Gmail Offline';
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
    }).catch(()=>showToast('Save failed!','error'));
  }

  function setAmount(v){ el('pay-amount').value=v; validateAmount(); }
  function validateAmount(){
    const v=parseFloat(el('pay-amount').value);
    el('gen-btn').disabled = !(v>0) || !gmailOK;
  }
  function generateQR(){
    const amt=parseFloat(el('pay-amount').value);
    if(!amt||amt<=0) return showToast('সঠিক অ্যামাউন্ট লিখুন!','error');
    fetch(`/api/create_order?amount=${amt}`)
      .then(r=>r.json()).then(d=>{
        if(d.status==='error'){ showToast(d.message,'error'); return; }
        currentOrderId = d.order_id;
        el('create-view').style.display='none';
        el('payment-view').style.display='block';
        el('pay-active').style.display='block';
        el('pay-success').style.display='none';
        el('order-info').innerText=`Total: ₹${Number(d.amount).toFixed(2)}`;
        el('secret-code-display').innerText=d.secret_code;
        el('qr-box').innerHTML='';
        el('verify-hint').innerText='';
        el('payment-status').className='status-pill status-waiting';
        el('payment-status').innerHTML='<span class="dots"><span></span><span></span><span></span></span> ব্যাংক কনফার্মেশনের অপেক্ষায়...';
        new QRCode(el('qr-box'),{text:d.upi_url,width:200,height:200,correctLevel:QRCode.CorrectLevel.M});
        startPolling(d.order_id);
      }).catch(()=>showToast('Order create failed!','error'));
  }

  /* ⚡ দ্রুত polling (1.5s) — কারণ সার্ভার cache সবসময় fresh */
  function startPolling(orderId){
    stopPolling();
    timeLeft=300; updateTimer();
    const waitStart=Date.now();
    timerInt=setInterval(()=>{ timeLeft--; updateTimer(); if(timeLeft<=0){ stopPolling(); showExpired(); } },1000);
    pollTimer=setInterval(()=>{
      fetch(`/api/verify_order?order_id=${orderId}`).then(r=>r.json()).then(d=>{
        if(d.status==='success'){ el('verify-hint').innerText=''; onVerified(d.record); }
        else if(d.status==='expired'){ stopPolling(); showExpired(); }
        else if(d.status==='cancelled'){ stopPolling(); resetQRView(); }
        else {
          if(Date.now()-waitStart>10000 && d.reason){
            el('verify-hint').innerText='ℹ️ '+(REASONS[d.reason]||'অপেক্ষা চলছে...');
          }
        }
      }).catch(()=>{});
    },1500);
  }
  function stopPolling(){ if(pollTimer)clearInterval(pollTimer); if(timerInt)clearInterval(timerInt); pollTimer=timerInt=null; }
  function updateTimer(){
    const bar=el('timer-bar');
    bar.style.width=(timeLeft/300*100)+'%';
    bar.classList.toggle('low',timeLeft<60);
    const m=Math.floor(timeLeft/60), s=timeLeft%60;
    el('timer-text').innerText=`Expires in ${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
  }
  function showExpired(){
    const st=el('payment-status');
    st.className='status-pill status-expired';
    st.innerText='⏰ Order expired! Dashboard-এ EXPIRED হিসেবে দেখা যাবে।';
  }

  function cancelOrder(){
    if(!currentOrderId){ resetQRView(); return; }
    fetch(`/api/cancel_order?order_id=${currentOrderId}`)
      .then(r=>r.json()).then(d=>{
        stopPolling();
        if(d.status==='success'){ showToast('❌ Order Cancelled — Dashboard-এ দেখা যাবে','error'); }
        resetQRView();
      }).catch(()=>showToast('Cancel failed!','error'));
  }

  function onVerified(rec){
    stopPolling(); lastRecord=rec;
    el('pay-active').style.display='none';
    el('pay-success').style.display='block';

    const payer = rec.payer || 'Customer';
    const amt = Number(rec.amount);

    el('thank-name').innerText = payer;
    countUp(el('thank-amount'), amt);
    el('thank-time').innerText = rec.time || new Date().toLocaleTimeString('en-IN',{hour12:true});

    el('success-details').innerHTML=`
      <div style="font-size:13px;color:var(--muted);margin-bottom:10px;">UTR: <span class="code-tag">${rec.utr}</span></div>`;

    playDing(); launchConfetti(); blinkTitle();
    loadStats(); loadHistory();
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

  function blinkTitle(){
    let n=0;
    const t=setInterval(()=>{
      document.title = n%2===0 ? '✅ Payment Received!' : 'BN Enterprises - Fuel POS';
      if(++n>6){ clearInterval(t); document.title='BN Enterprises - Fuel POS'; }
    },600);
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
  function resetQRView(){
    stopPolling(); currentOrderId=null;
    el('payment-view').style.display='none';
    el('create-view').style.display='block';
    el('pay-amount').value='';
    el('verify-hint').innerText='';
    validateAmount();
  }

  function loadHistory(){
    fetch('/api/history').then(r=>r.json()).then(list=>{
      historyData=list;
      renderTable(); renderRecent(); renderChart();
    });
  }
  function statusPill(s){
    if(s==='SUCCESS') return '<span class="st st-success">✅ SUCCESS</span>';
    if(s==='CANCELLED') return '<span class="st st-cancelled">❌ CANCELLED</span>';
    if(s==='EXPIRED') return '<span class="st st-expired">⏰ EXPIRED</span>';
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
    if(!rows.length){ tb.innerHTML='<tr><td colspan="7" class="empty-row">'+(q?'কোনো ম্যাচ পাওয়া যায়নি':'No transactions yet')+'</td></tr>'; return; }
    tb.innerHTML=[...rows].reverse().map(t=>{
      const idx=historyData.indexOf(t);
      return `<tr onclick='showReceipt(historyData[${idx}])' title="Receipt দেখতে/Download করতে ক্লিক করুন">
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
    if(!last.length){ box.innerHTML='<div style="color:var(--muted);font-size:13.5px;text-align:center;padding:14px">No verified transactions yet</div>'; return; }
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
    if(!last.length){ chart.innerHTML='<div style="color:var(--muted);font-size:13px">No data yet</div>'; return; }
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
      el('stat-today-count').innerText=`Today's Sales — ${d.today_count} txn`;
      el('stat-cancelled-sub').innerText=`Cancelled ${d.cancelled_count} · Expired ${d.expired_count}`;
    });
  }

  function showReceipt(rec){
    if(!rec) return;
    currentReceiptRec = rec;
    el('r-merchant').innerText=el('merchant-input').value;
    el('r-order').innerText=rec.order_id;
    el('r-code').innerText=rec.secret_code;
    el('r-payer').innerText=rec.payer||'UPI Customer';
    el('r-utr').innerText=(rec.status||'SUCCESS')==='SUCCESS'? rec.utr : '—';
    el('r-date').innerText=rec.date||'—';
    el('r-time').innerText=rec.time;
    el('r-amount').innerText='₹'+Number(rec.amount).toFixed(2);
    el('receipt-modal').classList.add('open');
  }
  function closeReceipt(){ el('receipt-modal').classList.remove('open'); }

  async function downloadReceipt(type){
    if(!currentReceiptRec) return showToast('আগে একটা Receipt খুলুন!','error');
    const node = el('receipt-print');
    const rec = currentReceiptRec;
    const fname = `Receipt_${rec.order_id}_${rec.secret_code}`;
    const overlay = el('dl-overlay'), dlText = el('dl-text');

    overlay.classList.add('open');
    dlText.innerText = type==='pdf' ? 'PDF তৈরি হচ্ছে...' : 'Image তৈরি হচ্ছে...';

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
        showToast('✅ PNG Downloaded!','success');
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
        showToast('✅ PDF Downloaded!','success');
      }
    }catch(e){
      console.error(e);
      el('receipt-modal').style.visibility='visible';
      showToast('❌ Download failed!','error');
    }finally{
      overlay.classList.remove('open');
    }
  }

  /* Init POS */
  checkEmailStatus(); loadHistory(); loadStats(); validateAmount(); syncSecurityToggle();
  setInterval(()=>{ if(el('tab-dash').classList.contains('active')) loadStats(); },10000);
  // প্রথম টাচে wake lock চালু (gesture requirement)
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
    return jsonify({"status": "success", "message": "✅ Settings updated successfully!"})

# ================= 🚨 Security APIs =================
@app.route('/api/security')
def api_security():
    return jsonify({"security": CONFIG["SECURITY_SWITCH"], "ack": ALARM_ACK})

@app.route('/api/alarm_stop', methods=['POST'])
def api_alarm_stop():
    global ALARM_ACK
    data = request.json or {}
    pin = str(data.get("pin", ""))
    if pin == str(CONFIG["SECURITY_PIN"]):
        CONFIG["SECURITY_SWITCH"] = False   # switch অটো OFF
        ALARM_ACK += 1
        return jsonify({"status": "ok", "ack": ALARM_ACK})
    return jsonify({"status": "error", "message": "ভুল PIN"}), 401

# ================= 🖥️ Display Device State =================
@app.route('/api/display_state')
def api_display_state():
    cleanup_expired_orders()
    st = {
        "mode": "idle",
        "merchant": CONFIG["MERCHANT_NAME"],
        "order_id": None, "amount": 0, "secret_code": "", "upi_url": "",
        "payer": None, "utr": "", "time": ""
    }
    if CURRENT_ORDER_ID and CURRENT_ORDER_ID in ACTIVE_ORDERS:
        o = ACTIVE_ORDERS[CURRENT_ORDER_ID]
        st["order_id"] = CURRENT_ORDER_ID
        st["amount"] = float(o["amount"])
        st["secret_code"] = o["secret_code"]
        if o["status"] == "PENDING":
            st["mode"] = "waiting"
            st["upi_url"] = (f"upi://pay?pa={quote(CONFIG['UPI_ID'])}"
                             f"&pn={quote(CONFIG['MERCHANT_NAME'])}"
                             f"&am={o['amount']}&cu=INR&tn={o['secret_code']}&tr={CURRENT_ORDER_ID}")
        elif o["status"] == "SUCCESS" and o.get("record"):
            rec = o["record"]
            st["mode"] = "success"
            st["payer"] = rec.get("payer")
            st["utr"] = rec.get("utr", "")
            st["time"] = rec.get("time", "")
        elif o["status"] == "CANCELLED":
            st["mode"] = "idle"
    return jsonify(st)

@app.route('/api/create_order')
def api_create_order():
    global CURRENT_ORDER_ID
    ok, msg = test_gmail_connection()
    if not ok:
        return jsonify({"status": "error", "message": msg}), 400

    try:
        amt = round(float(request.args.get("amount", 0)), 2)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid amount"}), 400
    if amt < 1 or amt > 100000:
        return jsonify({"status": "error", "message": "Amount must be ₹1 – ₹1,00,000"}), 400

    cleanup_expired_orders()

    pending_codes = {o["secret_code"] for o in ACTIVE_ORDERS.values() if o["status"] == "PENDING"}
    while True:
        code = "FP" + str(random.randint(100000, 999999))
        if code not in pending_codes:
            break

    order_id = "ORD" + uuid.uuid4().hex[:6].upper()
    upi_url = (f"upi://pay?pa={quote(CONFIG['UPI_ID'])}"
               f"&pn={quote(CONFIG['MERCHANT_NAME'])}"
               f"&am={amt}&cu=INR&tn={code}&tr={order_id}")

    ACTIVE_ORDERS[order_id] = {
        "amount": amt,
        "secret_code": code,
        "status": "PENDING",
        "created_at": time.time()
    }
    CURRENT_ORDER_ID = order_id   # 🆕 Display ডিভাইস সাথে সাথে দেখবে
    return jsonify({
        "order_id": order_id,
        "secret_code": code,
        "amount": amt,
        "upi_url": upi_url
    })

@app.route('/api/verify_order')
def api_verify_order():
    global CURRENT_ORDER_ID
    order_id = request.args.get('order_id', '')
    if order_id not in ACTIVE_ORDERS:
        return jsonify({"status": "error", "message": "Order not found"}), 404

    order = ACTIVE_ORDERS[order_id]

    if order["status"] == "SUCCESS":
        return jsonify({"status": "success", "record": order.get("record")})

    if order["status"] == "CANCELLED":
        return jsonify({"status": "cancelled"})

    cleanup_expired_orders()
    if order["status"] == "EXPIRED":
        return jsonify({"status": "expired"})

    strict = CONFIG.get("STRICT_CODE", False)
    verified, utr, payer, reason = verify_payment_mail(
        order["secret_code"], order["amount"], order["created_at"], strict)

    if verified and utr not in USED_UTRS:
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
        USED_UTRS.add(utr)
        PAYMENT_HISTORY.append(record)
        save_history()
        return jsonify({"status": "success", "record": record})

    if not verified:
        _log_debug(order_id, reason)

    return jsonify({"status": "waiting", "reason": reason})

@app.route('/api/cancel_order')
def api_cancel_order():
    order_id = request.args.get('order_id', '')
    if order_id not in ACTIVE_ORDERS:
        return jsonify({"status": "error", "message": "Order not found"}), 404

    order = ACTIVE_ORDERS[order_id]
    if order["status"] == "SUCCESS":
        return jsonify({"status": "error", "message": "Order already paid! Cancel করা যাবে না।"}), 400
    if order["status"] != "PENDING":
        return jsonify({"status": "error", "message": "Order already cancelled/expired"}), 400

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
    PAYMENT_HISTORY.append(record)
    save_history()
    return jsonify({"status": "success", "message": "Order cancelled"})

@app.route('/api/history')
def api_history():
    return jsonify(PAYMENT_HISTORY)

@app.route('/api/stats')
def api_stats():
    today = datetime.date.today().strftime("%Y-%m-%d")
    success_tx = [t for t in PAYMENT_HISTORY if t.get("status") == "SUCCESS"]
    today_tx = [t for t in success_tx if t.get("date") == today]
    today_sum = sum(float(t["amount"]) for t in today_tx)
    total_sum = sum(float(t["amount"]) for t in success_tx)
    cancelled_count = len([t for t in PAYMENT_HISTORY if t.get("status") == "CANCELLED"])
    expired_count = len([t for t in PAYMENT_HISTORY if t.get("status") == "EXPIRED"])
    total_orders = len(PAYMENT_HISTORY)
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
    writer.writerow(["Order ID", "Code", "Amount", "Status", "Payer Name", "UTR", "Date", "Time"])
    for t in PAYMENT_HISTORY:
        writer.writerow([t["order_id"], t["secret_code"], t["amount"], t.get("status", "SUCCESS"),
                         t.get("payer", ""), t.get("utr", ""), t.get("date", ""), t["time"]])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=transactions.csv"}
    )

# ================= Debug =================
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
