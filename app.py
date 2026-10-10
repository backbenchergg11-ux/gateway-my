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
    "REBATE_PERCENT": 0.75
}

PAYMENT_HISTORY = []
ACTIVE_ORDERS = {}
USED_UTRS = set()

ORDER_TIMEOUT = 300          # ৫ মিনিট পরে order expire
EMAIL_CACHE_TTL = 4          # Gmail কল থ্রটল (সেকেন্ড)
EMAIL_LOOKBACK_MIN = 20      # শেষ ২০ মিনিটের মেইল ভেরিফাই হবে
HISTORY_FILE = "payment_history.json"

_gmail_lock = threading.Lock()
_email_cache = []
_last_fetch = 0
_conn_cache = {"ok": False, "msg": "Not checked yet", "ts": 0}

# ================= হিস্টোরি সেভ / লোড (Restart-proof) =================
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
    except Exception:
        pass

load_history()

# ================= Gmail কানেকশন (থ্রটল সহ) =================
def test_gmail_connection():
    now = time.time()
    if now - _conn_cache["ts"] < 20:
        return _conn_cache["ok"], _conn_cache["msg"]
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(CONFIG["GMAIL_USER"], CONFIG["GMAIL_PASS"])
        mail.logout()
        _conn_cache.update(ok=True, msg="Gmail Connected & Active", ts=now)
    except Exception as e:
        _conn_cache.update(ok=False, msg=f"Gmail Error: {str(e)}", ts=now)
    return _conn_cache["ok"], _conn_cache["msg"]

# ================= ইমেইল বডি পার্সার (HTML সাপোর্ট সহ) =================
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

# ================= Payer Name Extractor (FamPay + সব ব্যাংক) =================
def extract_payer_name(subject, body):
    text = re.sub(r"\s+", " ", f"{subject} {body}")
    text = text.replace("*", " ").replace("#", " ").replace("_", " ")  # FamPay-র bold ** চিহ্ন বাদ

    # নামের পরে এসব শব্দ এলে নাম ওখানেই শেষ
    STOP = (r"(?=\s*(?:\(|\[|\bvia\b|\bref\b|\butr\b|\brrn\b|\btid\b|\btxn\b|\bid\b|\bno\b|\bon\b|"
            r"\btowards\b|\bfor\b|\bfrom\b|\bwith\b|\byour\b|\bupi\b|\bacc\b|\baccount\b|\ba/c\b|"
            r"\bbank\b|\bto\b|\bbalance\b|\bwallet\b|[,.\-|!?:;]|\d|$))")

    patterns = [
        # 🟣 FamPay: "You've received ₹500 from AARAV SHARMA"
        rf"(?:received|got|credited(?:\s+with)?)\s*(?:₹|rs\.?|inr)?\s*[\d,]+(?:\.\d{{1,2}})?\s*"
        rf"(?:from|by)\s+((?:mr\.?|mrs\.?|ms\.?|md\.?)?\s*[A-Za-z][A-Za-z\s\.']{{1,39}}?){STOP}",
        # 🟣 FamPay: "AARAV SHARMA sent you money"
        rf"([A-Za-z][A-Za-z\s\.']{{1,39}}?)\s+sent\s+you{STOP}",
        # 🟣 FamPay: "₹500 received from AARAV"
        rf"(?:₹|rs\.?|inr)\s*[\d,]+(?:\.\d{{1,2}})?\s*(?:received|credited)\s*from\s+"
        rf"([A-Za-z][A-Za-z\s\.']{{1,39}}?){STOP}",
        # 🏦 সাধারণ: "credited ... from NAME"
        rf"from\s+((?:mr\.?|mrs\.?|ms\.?|md\.?)?\s*[A-Za-z][A-Za-z\s\.']{{1,39}}?){STOP}",
        # 🏦 "Payer Name: XYZ" / "Sender Name: XYZ"
        r"(?:payer|sender|customer|remitter|beneficiary)\s*name\s*[:\-]\s*([A-Za-z][A-Za-z\s\.']{2,39})",
        # 🏦 "IMPS/Payment by NAME"
        rf"(?:payment|paid|transfer(?:red)?)\s+by\s+([A-Za-z][A-Za-z\s\.']{{1,39}}?){STOP}",
    ]

    # নাম নয় এমন প্রথম শব্দ
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
            if name.isupper():          # "AARAV SHARMA" → "Aarav Sharma"
                name = name.title()
            first_word = name.split()[0].lower() if name else ""
            if (len(name) >= 3
                    and first_word not in not_name_words
                    and not name.isdigit()
                    and not name.upper().startswith("FP")   # FP কোড নাম নয়
                    and "@" not in name):
                return name
    return None

# ================= রিসেন্ট মেইল ক্যাশ (Rate-limit protection) =================
def get_recent_emails():
    global _email_cache, _last_fetch
    with _gmail_lock:
        now = time.time()
        if now - _last_fetch < EMAIL_CACHE_TTL:
            return _email_cache
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(CONFIG["GMAIL_USER"], CONFIG["GMAIL_PASS"])
            mail.select("inbox")
            since = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
            _, messages = mail.search(None, f'(SINCE "{since}")')
            ids = messages[0].split()[-20:]
            parsed = []
            for m_id in reversed(ids):
                _, data = mail.fetch(m_id, "(RFC822)")
                for part in data:
                    if isinstance(part, tuple):
                        msg = email.message_from_bytes(part[1])
                        try:
                            raw_subj = msg.get("Subject", "")
                            decoded = email.header.decode_header(raw_subj)
                            subject = ""
                            for s, enc in decoded:
                                if isinstance(s, bytes):
                                    subject += s.decode(enc or "utf-8", errors="ignore")
                                else:
                                    subject += str(s)
                        except Exception:
                            subject = str(msg.get("Subject", ""))
                        body = _parse_body(msg)
                        parsed.append((msg, subject, body))
            mail.logout()
            _email_cache = parsed
            _last_fetch = now
        except Exception:
            pass
    return _email_cache

def _is_recent_mail(msg):
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        if not dt:
            return True
        age = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
        return -120 < age < EMAIL_LOOKBACK_MIN * 60
    except Exception:
        return True

# ================= পেমেন্ট ভেরিফিকেশন (কোড + অ্যামাউন্ট + Payer Name) =================
def verify_payment_mail(secret_code, expected_amount):
    emails = get_recent_emails()
    target = str(secret_code).strip().lower()
    expected = round(float(expected_amount), 2)

    for msg, subject, body in emails:
        if not _is_recent_mail(msg):
            continue
        low = body.lower()
        if target not in low and target not in subject.lower():
            continue
        if not any(w in low for w in ["credited", "received", "deposited", "upi"]):
            continue
        amounts = re.findall(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d{1,2})?)", body, re.IGNORECASE)
        for a in amounts:
            try:
                val = float(a.replace(",", ""))
            except ValueError:
                continue
            if abs(expected - val) < 0.01:
                m = re.search(
                    r"(?:ref(?:\s*(?:no|number)?)?|rrn|utr|upi\s*ref)[:\s./]*([0-9]{10,14})",
                    body, re.IGNORECASE)
                utr = m.group(1) if m else "UPI-" + secret_code
                payer = extract_payer_name(subject, body)
                return True, utr, payer
    return False, None, None

# ================= Expired অর্ডার → History-তে যাবে =================
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

# ================= Merchant UI (BN Enterprises) =================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BN Enterprises - Fuel POS Terminal</title>
<script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
<style>
*{box-sizing:border-box}
:root{--bg:#f1f5f9;--card:#fff;--primary:#2563eb;--primary-dark:#1d4ed8;--success:#16a34a;--danger:#dc2626;--text:#0f172a;--muted:#64748b;--border:#e2e8f0}
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
input{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--border);background:#f8fafc;font-size:15px;margin-bottom:14px;outline:none;transition:.2s}
input:focus{border-color:var(--primary);background:#fff;box-shadow:0 0 0 3px rgba(37,99,235,.12)}
.btn{background:var(--primary);color:#fff;border:none;padding:13px 20px;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;width:100%;transition:.2s}
.btn:hover{background:var(--primary-dark);transform:translateY(-1px)}
.btn:disabled{background:#94a3b8;cursor:not-allowed;transform:none}
.btn.gray{background:#64748b;margin-top:12px}
.btn.red{background:var(--danger);margin-top:12px}
.btn.dark{background:#334155}
.btn.small{width:auto;padding:9px 16px;font-size:13px}

.quick-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.quick-btn{flex:1;min-width:60px;padding:9px 0;border:1px solid var(--border);background:#f8fafc;border-radius:9px;font-weight:700;font-size:13px;cursor:pointer;color:var(--text);transition:.2s}
.quick-btn:hover{border-color:var(--primary);color:var(--primary);background:#eff6ff}

.qr-display{text-align:center}
#qr-box{padding:14px;background:#fff;border:2px dashed #cbd5e1;border-radius:14px;display:inline-block;margin:10px auto;animation:qrPop .55s cubic-bezier(.34,1.56,.64,1)}
.rebate-hint{background:#fef3c7;color:#b45309;border:1px solid #fde68a;border-radius:9px;font-size:12.5px;font-weight:600;padding:7px;margin:10px 0}

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

/* 🙏 Thank You Card */
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

/* 🖥️ Fullscreen Customer Display */
.cust-display{position:fixed;inset:0;background:linear-gradient(-45deg,#064e3b,#059669,#10b981,#047857);background-size:400% 400%;animation:gradientShift 8s ease infinite;z-index:9999;display:none;flex-direction:column;align-items:center;justify-content:center;text-align:center;color:#fff;padding:24px;cursor:pointer}
.cust-display.open{display:flex}
.cd-logo{font-size:20px;font-weight:800;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.3);padding:10px 24px;border-radius:9999px}
.cd-check{font-size:70px;margin:24px 0 8px;animation:cdPop .7s cubic-bezier(.34,1.56,.64,1)}
.cd-thanks{font-size:38px;font-weight:900}
.cd-name{font-size:44px;font-weight:900;margin:8px 0;animation:namePop .9s .3s cubic-bezier(.34,1.56,.64,1) both;text-shadow:0 4px 20px rgba(0,0,0,.25);word-break:break-word}
.cd-msg{font-size:18px;font-weight:600;opacity:.92}
.cd-amount{font-size:62px;font-weight:900;margin:18px 0 4px;font-variant-numeric:tabular-nums;animation:cdPop .8s .5s cubic-bezier(.34,1.56,.64,1) both;text-shadow:0 6px 24px rgba(0,0,0,.3)}
.cd-time{font-size:16px;font-weight:600;opacity:.9}
.cd-hint{position:absolute;bottom:26px;font-size:12.5px;opacity:.65}
@keyframes cdPop{0%{transform:scale(.4);opacity:0}60%{transform:scale(1.12)}100%{transform:scale(1);opacity:1}}

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

#toast-wrap{position:fixed;bottom:22px;right:22px;display:flex;flex-direction:column;gap:10px;z-index:10000}
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
.r-green{color:var(--success)}
.modal-actions{display:flex;gap:10px;margin-top:16px;justify-content:center}

.confetti{position:fixed;top:-12px;z-index:9999;pointer-events:none}

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
<div class="container">

  <div class="header-card">
    <div class="merchant-title">
      <div class="merchant-logo">⛽</div>
      <div>
        <h2 style="margin:0;font-size:20px;" id="hdr-merchant">{{ config.MERCHANT_NAME }}</h2>
        <div style="font-size:12.5px;opacity:.85;margin-top:2px;">UPI Auto-Verify Engine v3.0</div>
      </div>
    </div>
    <div class="badge-group">
      <div class="badge badge-glass">🏷️ {{ config.REBATE_PERCENT }}% Instant Rebate</div>
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
        <button class="btn dark" onclick="updateSettings()">💾 Save Settings</button>
        <div style="margin-top:16px;background:#f8fafc;border:1px solid var(--border);border-radius:10px;padding:12px;font-size:12.5px;color:var(--muted);line-height:1.6">
          ℹ️ কাস্টমার QR স্ক্যান করলে UPI নোটে <b style="color:var(--primary)">FP-code</b> অটো যুক্ত হবে। পেমেন্ট হলে Gmail-এ ব্যাংকের মেইলে <b>কোড + অ্যামাউন্ট + কাস্টমারের নাম</b> অটো-ডিটেক্ট হবে।
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
            <div class="rebate-hint">🎁 কাস্টমার পাবে {{ config.REBATE_PERCENT }}% ইনস্ট্যান্ট রিবেট</div>
            <div id="qr-box"></div>
            <div id="payment-status" class="status-pill status-waiting"><span class="dots"><span></span><span></span><span></span></span> ব্যাংক কনফার্মেশনের অপেক্ষায়...</div>
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

            <button class="btn" style="background:var(--success)" onclick="openCustomerDisplay()">🖥️ Customer Display (Full Screen)</button>
            <button class="btn" onclick="showReceipt(lastRecord)">🧾 View Receipt</button>
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
      <div class="stat-card"><div class="stat-icon" style="background:#fffbeb">🎁</div><div><div class="stat-val" id="stat-rebate">₹0.00</div><div class="stat-sub">Total Rebate Given</div></div></div>
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

<!-- 🖥️ Fullscreen Customer Display -->
<div id="cust-display" class="cust-display" onclick="closeCustomerDisplay()">
  <div class="cd-logo">⛽ <span id="cd-merchant">BN Enterprises</span></div>
  <div class="cd-check">✅</div>
  <div class="cd-thanks">Thank You!</div>
  <div class="cd-name"><span style="opacity:.75">@</span><span id="cd-name">Customer</span></div>
  <div class="cd-msg">আপনার পেমেন্ট সফল হয়েছে · Payment Successful</div>
  <div class="cd-amount" id="cd-amount">₹0.00</div>
  <div class="cd-time">🕐 <span id="cd-time"></span></div>
  <div class="cd-hint">👆 ট্যাপ করলে বন্ধ হবে · ১৫ সেকেন্ডে অটো-বন্ধ</div>
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
      <div class="r-row r-green"><span>🎁 Rebate ({{ config.REBATE_PERCENT }}%)</span><b id="r-rebate"></b></div>
      <div class="r-sep"></div>
      <div class="r-sub">✅ Verified via Gmail Auto-Engine</div>
      <div class="r-sub">ধন্যবাদ! আবার আসবেন 🙏</div>
    </div>
    <div class="modal-actions">
      <button class="btn small" style="background:var(--success)" onclick="window.print()">🖨️ Print</button>
      <button class="btn small" style="background:#64748b" onclick="closeReceipt()">Close</button>
    </div>
  </div>
</div>

<div id="toast-wrap"></div>

<script>
let pollTimer=null, timerInt=null, timeLeft=300, lastRecord=null, historyData=[], gmailOK=false, currentOrderId=null, statusFilter='ALL', cdTimer=null;
const el = id => document.getElementById(id);

/* ---- Live Clock ---- */
function updateClock(){ el('clock').innerText = new Date().toLocaleTimeString('en-IN',{hour12:true}); }
setInterval(updateClock,1000); updateClock();

/* ---- Tabs ---- */
function switchTab(name){
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(s=>s.classList.remove('active'));
  el('btn-'+name).classList.add('active');
  el('tab-'+name).classList.add('active');
  if(name==='dash') refreshDashboard();
}
function refreshDashboard(){ loadStats(); loadHistory(); }

/* ---- Toast ---- */
function showToast(msg,type='success'){
  const t=document.createElement('div');
  t.className='toast '+type; t.innerText=msg;
  el('toast-wrap').appendChild(t);
  setTimeout(()=>t.classList.add('show'),20);
  setTimeout(()=>{t.classList.remove('show'); setTimeout(()=>t.remove(),400);},3200);
}

/* ---- Gmail Status ---- */
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
setInterval(checkEmailStatus,30000);

/* ---- Settings ---- */
function updateSettings(){
  fetch('/api/update_settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({upi_id:el('upi-input').value,merchant_name:el('merchant-input').value})})
  .then(r=>r.json()).then(d=>{
    el('hdr-merchant').innerText=el('merchant-input').value;
    showToast(d.message,'success');
  }).catch(()=>showToast('Save failed!','error'));
}

/* ---- QR Generation ---- */
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
      el('payment-status').className='status-pill status-waiting';
      el('payment-status').innerHTML='<span class="dots"><span></span><span></span><span></span></span> ব্যাংক কনফার্মেশনের অপেক্ষায়...';
      new QRCode(el('qr-box'),{text:d.upi_url,width:200,height:200,correctLevel:QRCode.CorrectLevel.M});
      startPolling(d.order_id);
    }).catch(()=>showToast('Order create failed!','error'));
}

/* ---- Polling + Timer ---- */
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
  },2500);
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

/* ---- Cancel Order → Dashboard-এ যাবে ---- */
function cancelOrder(){
  if(!currentOrderId){ resetQRView(); return; }
  fetch(`/api/cancel_order?order_id=${currentOrderId}`)
    .then(r=>r.json()).then(d=>{
      stopPolling();
      if(d.status==='success'){ showToast('❌ Order Cancelled — Dashboard-এ দেখা যাবে','error'); }
      resetQRView();
    }).catch(()=>showToast('Cancel failed!','error'));
}

/* ---- Success: Thank @Name + Amount Count-up + Time ---- */
function onVerified(rec){
  stopPolling(); lastRecord=rec;
  el('pay-active').style.display='none';
  el('pay-success').style.display='block';

  const payer = rec.payer || 'Customer';
  const amt = Number(rec.amount);

  el('thank-name').innerText = payer;                        // 🙏 Thank @name
  countUp(el('thank-amount'), amt);                          // 💰 Amount count-up
  el('thank-time').innerText = rec.time || new Date().toLocaleTimeString('en-IN',{hour12:true}); // 🕐 Time

  const rebate=(amt*0.75/100).toFixed(2);
  el('success-details').innerHTML=`
    <div style="font-size:13px;color:var(--muted);margin-bottom:10px;">UTR: <span class="code-tag">${rec.utr}</span></div>
    <div style="font-size:13px;color:#15803d;margin-bottom:10px;font-weight:600;">🎁 কাস্টমার রিবেট: ₹${rebate}</div>`;

  playSuccessSound(); launchConfetti(); blinkTitle();
  loadStats(); loadHistory();

  // 🖥️ ১.৫ সেকেন্ড পরে অটো Fullscreen Customer Display খুলবে
  setTimeout(()=>openCustomerDisplay(payer, amt, rec.time), 1500);
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

/* ---- 🖥️ Fullscreen Customer Display ---- */
function openCustomerDisplay(payer, amt, time){
  if(lastRecord){
    payer = payer || lastRecord.payer || 'Customer';
    amt = amt || Number(lastRecord.amount);
    time = time || lastRecord.time;
  }
  el('cd-merchant').innerText = el('merchant-input').value;
  el('cd-name').innerText = payer || 'Customer';
  el('cd-time').innerText = time || '';
  countUp(el('cd-amount'), amt || 0);
  el('cust-display').classList.add('open');
  playSuccessSound();
  if(cdTimer) clearTimeout(cdTimer);
  cdTimer=setTimeout(closeCustomerDisplay, 15000);   // ১৫ সেকেন্ড পরে অটো-বন্ধ
}
function closeCustomerDisplay(){
  el('cust-display').classList.remove('open');
  if(cdTimer){ clearTimeout(cdTimer); cdTimer=null; }
}

/* ---- Success Effects ---- */
function blinkTitle(){
  let n=0;
  const t=setInterval(()=>{
    document.title = n%2===0 ? '✅ Payment Received!' : 'BN Enterprises - Fuel POS';
    if(++n>6){ clearInterval(t); document.title='BN Enterprises - Fuel POS'; }
  },600);
}
function playSuccessSound(){
  try{
    const ctx=new (window.AudioContext||window.webkitAudioContext)();
    if(ctx.state==='suspended') ctx.resume();
    [523.25,659.25,783.99,1046.5].forEach((f,i)=>{
      const o=ctx.createOscillator(),g=ctx.createGain();
      o.type='sine'; o.frequency.value=f;
      const t0=ctx.currentTime+i*0.12;
      g.gain.setValueAtTime(0.25,t0);
      g.gain.exponentialRampToValueAtTime(0.01,t0+0.35);
      o.connect(g); g.connect(ctx.destination);
      o.start(t0); o.stop(t0+0.4);
    });
  }catch(e){}
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
  validateAmount();
}

/* ---- History / Stats ---- */
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
    return `<tr onclick='showReceipt(historyData[${idx}])' title="Receipt দেখতে ক্লিক করুন">
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
    animateNum(el('stat-rebate'),d.total_rebate);
    animateNum(el('stat-avg'),d.avg_sale);
    animateNum(el('stat-cancelled'),d.cancelled_expired,'');
    animateNum(el('stat-rate'),d.success_rate,'','%' );
    el('stat-today-count').innerText=`Today's Sales — ${d.today_count} txn`;
    el('stat-cancelled-sub').innerText=`Cancelled ${d.cancelled_count} · Expired ${d.expired_count}`;
  });
}

/* ---- Receipt ---- */
function showReceipt(rec){
  if(!rec) return;
  el('r-merchant').innerText=el('merchant-input').value;
  el('r-order').innerText=rec.order_id;
  el('r-code').innerText=rec.secret_code;
  el('r-payer').innerText=rec.payer||'UPI Customer';
  el('r-utr').innerText=(rec.status||'SUCCESS')==='SUCCESS'? rec.utr : '—';
  el('r-date').innerText=rec.date||'—';
  el('r-time').innerText=rec.time;
  el('r-amount').innerText='₹'+Number(rec.amount).toFixed(2);
  el('r-rebate').innerText='₹'+(Number(rec.amount)*0.75/100).toFixed(2);
  el('receipt-modal').classList.add('open');
}
function closeReceipt(){ el('receipt-modal').classList.remove('open'); }

/* ---- Init ---- */
checkEmailStatus(); loadHistory(); loadStats(); validateAmount();
setInterval(()=>{ if(el('tab-dash').classList.contains('active')) loadStats(); },10000);
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
    return jsonify({"status": "success", "message": "✅ Settings updated successfully!"})

@app.route('/api/create_order')
def api_create_order():
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
    return jsonify({
        "order_id": order_id,
        "secret_code": code,
        "amount": amt,
        "upi_url": upi_url,
        "rebate": round(amt * CONFIG["REBATE_PERCENT"] / 100, 2)
    })

@app.route('/api/verify_order')
def api_verify_order():
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

    verified, utr, payer = verify_payment_mail(order["secret_code"], order["amount"])

    if verified and utr not in USED_UTRS:
        now = datetime.datetime.now()
        record = {
            "order_id": order_id,
            "secret_code": order["secret_code"],
            "amount": float(order["amount"]),
            "status": "SUCCESS",
            "payer": payer,          # 🆕 Email থেকে পাওয়া কাস্টমারের নাম
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

    return jsonify({"status": "waiting"})

# ================= Cancel Order API =================
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
        "today_rebate": round(today_sum * CONFIG["REBATE_PERCENT"] / 100, 2),
        "total_rebate": round(total_sum * CONFIG["REBATE_PERCENT"] / 100, 2),
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

# ================= Debug: শেষ ইমেইলগুলোতে কী ধরা পড়ছে দেখুন =================
@app.route('/api/debug/emails')
def api_debug_emails():
    emails = get_recent_emails()
    result = []
    for msg, subject, body in emails[:10]:
        result.append({
            "subject": subject[:80],
            "from": str(msg.get("From", ""))[:60],
            "date": str(msg.get("Date", ""))[:30],
            "extracted_payer": extract_payer_name(subject, body),   # 👈 এই নামটাই দেখাবে
            "body_preview": body[:300]
        })
    return jsonify(result)

# ================= Debug: নিজের টেক্সট দিয়ে নাম-extraction টেস্ট =================
@app.route('/api/debug/test_name', methods=['POST'])
def api_debug_test_name():
    data = request.json or {}
    text = data.get("text", "")
    return jsonify({
        "input": text[:500],
        "extracted_payer": extract_payer_name("", text)
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
