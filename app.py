
import imaplib
import email
from email.utils import parsedate_to_datetime
import html
import re
import threading
import time
import json
import os
import random
import socket
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, make_response

socket.setdefaulttimeout(10)

app = Flask(__name__)
CONFIG_FILE = "config.json"
TXN_FILE = "transactions.json"
USED_UTRS_FILE = "used_utrs.json"

def load_json(file, default):
    if os.path.exists(file):
        try:
            with open(file, "r") as f:
                return json.load(f)
        except:
            pass
    return default

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=2)

CONFIG = load_json(CONFIG_FILE, {
    "email": "",
    "app_password": "",
    "is_connected": False,
    "upi_id": "verifiedtanmay@fam"
})
TRANSACTIONS = load_json(TXN_FILE, [])
USED_UTRS = set(load_json(USED_UTRS_FILE, []))
LIVE_ORDERS = {}

BLOCKED_NAMES = ["fam", "fampay", "team fam", "your", "bank", "account", "support", "customer"]
IGNORED_DOMAINS = ["gmail.com", "google.com", "fampay.in", "hdfcbank.net", "icicibank.com", "sbi.co.in", "axisbank.com"]

@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        res = make_response()
        res.headers["Access-Control-Allow-Origin"] = "*"
        res.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        res.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return res

@app.after_request
def apply_headers(res):
    res.headers["Access-Control-Allow-Origin"] = "*"
    res.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    res.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return res

# --- নিখুঁত টেক্সট এক্সট্রাক্টর (HTML ট্যাগ ও স্পেস দূরীকরণ) ---
def clean_email_html(raw_payload):
    try:
        text = html.unescape(raw_payload)
        text = re.sub(r'<style.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<script.*?</script>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        return " ".join(text.split())
    except:
        return raw_payload

def extract_fampay_data(body_clean, current_upi):
    # ১. টাকার পরিমাণ বের করা
    amt_val = None
    amt_matches = re.findall(r'(?:inr|rs\.?|₹)\s*([\d,]+(?:\.\d{1,2})?)', body_clean, re.IGNORECASE)
    if not amt_matches:
        amt_matches = re.findall(r'received\s*(?:inr|rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)', body_clean, re.IGNORECASE)
    if amt_matches:
        try:
            amt_val = float(amt_matches[0].replace(',', ''))
        except:
            pass

    # ২. UTR নম্বর
    utr_matches = re.findall(r'\b(\d{12})\b', body_clean)
    utr_val = utr_matches[0] if utr_matches else f"FAM{int(time.time()*1000)[-8:]}"

    # ৩. প্রেরকের নাম বের করা
    sender_name = "Customer"
    fam_match = re.search(r'from\s+(?:Mr\.?|Mrs\.?|Ms\.?)?\s*([A-Za-z\s]{2,30}?)(?:\s*(?:\.|\,|\!|\n|on|using|via|with|utr|ref|upi|$))', body_clean, re.IGNORECASE)
    if fam_match:
        c = fam_match.group(1).strip()
        if c.lower() not in BLOCKED_NAMES and len(c) > 2:
            sender_name = c

    # ৪. প্রেরকের UPI আইডি
    sender_upi = "UPI Verified"
    vpas = re.findall(r'([a-zA-Z0-9.\-_]{2,35}@[a-zA-Z0-9]{2,20})', body_clean)
    for v in vpas:
        v_clean = v.strip()
        domain = v_clean.split('@')[-1].lower()
        if v_clean.lower() != current_upi.lower() and domain not in IGNORED_DOMAINS:
            sender_upi = v_clean
            break

    return amt_val, utr_val, sender_name, sender_upi

# --- ⚡ আল্ট্রা-রিয়েলটাইম ইমেইল স্ক্যানার ---
def instant_email_worker():
    global USED_UTRS, TRANSACTIONS
    mail = None

    while True:
        if not CONFIG.get("is_connected"):
            time.sleep(1)
            continue

        pending_orders = [oid for oid, ord in LIVE_ORDERS.items() if ord["status"] == "PENDING"]

        if pending_orders:
            try:
                if mail is None:
                    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
                    mail.login(CONFIG["email"], CONFIG["app_password"])

                # ইনবক্স রি-সিঙ্ক (নতুন ইমেইল তৎক্ষণাৎ পাওয়ার জন্য)
                mail.select("inbox")
                status, messages = mail.search(None, 'ALL')

                if status == 'OK':
                    email_ids = messages[0].split()
                    latest_ids = email_ids[-6:] if len(email_ids) >= 6 else email_ids

                    for e_id in reversed(latest_ids):
                        _, data = mail.fetch(e_id, "(RFC822)")
                        msg = email.message_from_bytes(data[0][1])

                        try:
                            email_dt = parsedate_to_datetime(msg.get("Date"))
                            email_ts = email_dt.timestamp()
                        except:
                            email_ts = time.time()

                        body_parts = []
                        for part in msg.walk():
                            if part.get_content_type() in ["text/plain", "text/html"]:
                                try:
                                    payload = part.get_payload(decode=True).decode(errors="ignore")
                                    body_parts.append(clean_email_html(payload))
                                except:
                                    pass

                        body_clean = " ".join(body_parts)
                        body_lower = body_clean.lower()

                        if any(kw in body_lower for kw in ["credited", "received", "deposited", "fampay", "upi ref"]):
                            amt_val, utr_val, sender_name, sender_upi = extract_fampay_data(body_clean, CONFIG.get("upi_id", ""))

                            if amt_val is not None:
                                for oid in pending_orders:
                                    ord_data = LIVE_ORDERS[oid]

                                    # টাইম বাফার ৩ মিনিট (ঘড়ির সময়ের পার্থক্যে যাতে না আটকে যায়)
                                    time_valid = email_ts >= (ord_data["created_at"] - 180)
                                    amt_valid = abs(ord_data["amount"] - amt_val) < 0.01

                                    if amt_valid and time_valid:
                                        if utr_val in USED_UTRS:
                                            continue

                                        # 🎯 পেমেন্ট ম্যাচ সম্পন্ন!
                                        ord_data["status"] = "PAID"
                                        ord_data["utr"] = utr_val
                                        ord_data["sender_name"] = sender_name
                                        ord_data["sender_upi"] = sender_upi

                                        USED_UTRS.add(utr_val)
                                        save_json(USED_UTRS_FILE, list(USED_UTRS))

                                        txn_entry = {
                                            "order_id": oid,
                                            "amount": amt_val,
                                            "sender_name": sender_name,
                                            "sender_upi": sender_upi,
                                            "utr": utr_val,
                                            "time": datetime.now().strftime("%I:%M %p")
                                        }
                                        TRANSACTIONS.insert(0, txn_entry)
                                        TRANSACTIONS = TRANSACTIONS[:15]
                                        save_json(TXN_FILE, TRANSACTIONS)

                                        print(f"\n==========================================")
                                        print(f" [✓] ভেরিফাইড! টাকা: ₹{amt_val}")
                                        print(f" [✓] প্রেরক: {sender_name} ({sender_upi})")
                                        print(f" [✓] UTR / Ref: {utr_val}")
                                        print(f"==========================================\n")
                                        break

            except Exception as e:
                try:
                    mail.logout()
                except:
                    pass
                mail = None
                time.sleep(1)

        time.sleep(0.5)

threading.Thread(target=instant_email_worker, daemon=True).start()

# --- BACKEND ENDPOINTS ---
@app.route('/api/create', methods=['POST'])
def api_create():
    data = request.json or {}
    amount = float(data.get('amount', 0))

    if not CONFIG.get("is_connected"):
        return jsonify({"success": False, "message": "আগে ইমেইল কানেক্ট করুন!"}), 400

    order_id = f"PAY{random.randint(100000, 999999)}"
    current_upi = CONFIG.get("upi_id", "verifiedtanmay@fam")
    
    LIVE_ORDERS[order_id] = {
        "order_id": order_id,
        "amount": amount,
        "upi_id": current_upi,
        "status": "PENDING",
        "created_at": time.time(),
        "utr": None,
        "sender_name": "Checking...",
        "sender_upi": "Checking..."
    }
    print(f"\n[+] নতুন অর্ডার তৈরি: {order_id} | পরিমাণ: ₹{amount}")
    return jsonify({"success": True, "order_id": order_id, "amount": amount, "upi_id": current_upi})

@app.route('/api/check', methods=['GET'])
def api_check():
    order_id = request.args.get('order_id')
    if not order_id or order_id not in LIVE_ORDERS:
        return jsonify({"status": "NOT_FOUND"}), 404

    order = LIVE_ORDERS[order_id]
    if order["status"] == "PENDING" and (time.time() - order["created_at"]) > 60:
        order["status"] = "EXPIRED"

    return jsonify({
        "order_id": order_id,
        "status": order["status"],
        "utr": order["utr"],
        "amount": order["amount"],
        "sender_name": order.get("sender_name", "Customer"),
        "sender_upi": order.get("sender_upi", "N/A")
    })

@app.route('/api/save-upi', methods=['POST'])
def api_save_upi():
    data = request.json or {}
    new_upi = data.get('upi_id', '').strip()
    if not new_upi or '@' not in new_upi:
        return jsonify({"success": False, "message": "সঠিক UPI ID দিন!"}), 400

    CONFIG["upi_id"] = new_upi
    save_json(CONFIG_FILE, CONFIG)
    return jsonify({"success": True, "message": "UPI ID সেভ হয়েছে!", "upi_id": new_upi})

@app.route('/api/transactions', methods=['GET'])
def api_transactions():
    return jsonify({"success": True, "transactions": TRANSACTIONS})

@app.route('/api/config', methods=['POST'])
def api_config():
    data = request.json or {}
    mail_user = data.get('email', '').strip()
    mail_pass = data.get('app_password', '').strip().replace(" ", "")

    try:
        test = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        test.login(mail_user, mail_pass)
        test.logout()

        CONFIG["email"] = mail_user
        CONFIG["app_password"] = mail_pass
        CONFIG["is_connected"] = True
        save_json(CONFIG_FILE, CONFIG)
        return jsonify({"success": True, "message": "ইমেইল সফলভাবে কানেক্ট হয়েছে!"})
    except Exception:
        return jsonify({"success": False, "message": "লগইন ব্যর্থ! App Password চেক করুন।"}), 401

@app.route('/api/status', methods=['GET'])
def api_status():
    return jsonify({
        "email_connected": CONFIG.get("is_connected", False),
        "email_user": CONFIG.get("email", ""),
        "upi_id": CONFIG.get("upi_id", "verifiedtanmay@fam")
    })

# --- FRONTEND UI ---
HTML_PAGE = """
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Instant UPI Gateway</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        body { background: #f8fafc; color: #0f172a; display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 16px; }
        .container { width: 100%; max-width: 420px; display: flex; flex-direction: column; gap: 14px; }
        .card { background: #fff; border: 1px solid #e2e8f0; border-radius: 24px; padding: 22px; box-shadow: 0 10px 30px -5px rgba(0,0,0,0.05); text-align: center; }
        .status-pill { display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; border-radius: 20px; font-size: 11px; font-weight: 700; margin-bottom: 12px; }
        .connected { background: #dcfce7; color: #15803d; }
        .disconnected { background: #fee2e2; color: #b91c1c; }
        h2 { font-size: 1.4rem; font-weight: 800; color: #0f172a; margin-bottom: 4px; }
        p { font-size: 13px; color: #64748b; margin-bottom: 14px; }
        .settings-btn { font-size: 12px; color: #2563eb; cursor: pointer; display: inline-block; margin-bottom: 12px; font-weight: 600; }
        #settingsBox { display: none; background: #f8fafc; border: 1px solid #e2e8f0; padding: 12px; border-radius: 12px; margin-bottom: 14px; text-align: left; }
        #settingsBox input { width: 100%; padding: 10px; margin-bottom: 8px; background: #fff; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 13px; }
        .upi-edit-card { background: #eff6ff; border: 1px dashed #2563eb; padding: 10px 12px; border-radius: 14px; margin-bottom: 16px; text-align: left; }
        .upi-label { font-size: 11px; font-weight: 700; color: #1e40af; display: flex; justify-content: space-between; margin-bottom: 6px; }
        .upi-row { display: flex; gap: 6px; }
        .upi-input { flex: 1; padding: 8px 10px; border: 1px solid #93c5fd; background: #fff; border-radius: 8px; font-size: 13px; font-weight: 700; color: #1d4ed8; outline: none; }
        .btn-save-upi { background: #2563eb; color: #fff; border: none; padding: 8px 14px; border-radius: 8px; font-size: 12px; font-weight: 700; cursor: pointer; }
        .amt-input { width: 100%; padding: 14px; background: #f8fafc; border: 2px solid #e2e8f0; border-radius: 14px; font-size: 24px; font-weight: 800; text-align: center; outline: none; margin-bottom: 14px; }
        .amt-input:focus { border-color: #2563eb; background: #fff; }
        .chips { display: flex; gap: 8px; justify-content: center; margin-bottom: 16px; }
        .chip { background: #f1f5f9; border: 1px solid #cbd5e1; padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 700; cursor: pointer; color: #334155; }
        .chip:hover { background: #2563eb; color: #fff; border-color: #2563eb; }
        .btn-main { width: 100%; padding: 14px; background: #2563eb; border: none; border-radius: 14px; color: #fff; font-size: 15px; font-weight: 700; cursor: pointer; }
        .btn-main:hover { background: #1d4ed8; }
        #gatewayModal { display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(15, 23, 42, 0.55); backdrop-filter: blur(8px); z-index: 99999; align-items: center; justify-content: center; padding: 16px; }
        .modal-box { background: #fff; border-radius: 26px; width: 100%; max-width: 380px; padding: 24px; text-align: center; box-shadow: 0 20px 45px rgba(0,0,0,0.18); }
        .qr-stage { position: relative; background: #fff; padding: 12px; border-radius: 18px; display: inline-block; border: 2px solid #e2e8f0; overflow: hidden; margin: 10px 0; }
        .qr-stage::after { content: ''; position: absolute; left: 0; top: 0; width: 100%; height: 3px; background: linear-gradient(90deg, transparent, #2563eb, #38bdf8, transparent); animation: laser 2s ease-in-out infinite alternate; }
        @keyframes laser { 0% { top: 6%; } 100% { top: 92%; } }
        .receipt-card { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 16px; padding: 14px; font-size: 13px; text-align: left; margin: 14px 0; }
        .receipt-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #f1f5f9; }
        .receipt-row:last-child { border-bottom: none; }
        .receipt-row span:first-child { color: #64748b; font-weight: 500; }
        .receipt-row span:last-child { color: #0f172a; font-weight: 700; }
        .history-card { text-align: left; }
        .history-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .history-head h3 { font-size: 13px; font-weight: 800; color: #0f172a; }
        .txn-table { width: 100%; border-collapse: collapse; font-size: 12px; }
        .txn-table th { padding: 6px; text-align: left; color: #64748b; border-bottom: 1px solid #e2e8f0; }
        .txn-table td { padding: 8px 6px; border-bottom: 1px solid #f1f5f9; }
        .txn-amt { color: #16a34a; font-weight: 800; }
    </style>
</head>
<body>

<div class="container">
    <div class="card">
        <div id="connStatus" class="status-pill disconnected">🔴 Checking Backend...</div>
        <h2>Instant UPI Terminal</h2>
        <p>অ্যামাউন্ট লিখুন ও পেমেন্ট রিসিভ করুন</p>

        <a class="settings-btn" onclick="toggleSettings()">⚙️ ইমেইল সেটিংস কনফিগার</a>
        <div id="settingsBox">
            <input type="email" id="emailInput" placeholder="Gmail ID">
            <input type="password" id="passInput" placeholder="16-Digit Google App Password">
            <button class="btn-main" style="padding: 8px; font-size: 12px;" onclick="saveEmail()">কানেক্ট করুন</button>
        </div>

        <div class="upi-edit-card">
            <div class="upi-label">
                <span>RECEIVER UPI ID</span>
                <span id="saveStatus" style="color: #16a34a; font-weight: 600;"></span>
            </div>
            <div class="upi-row">
                <input type="text" id="upiInput" class="upi-input" placeholder="yourname@fam">
                <button class="btn-save-upi" onclick="saveUpiId()">Save</button>
            </div>
        </div>

        <input type="number" id="payAmt" class="amt-input" placeholder="0.00" value="3" step="0.01">

        <div class="chips">
            <span class="chip" onclick="setAmt(3)">₹3</span>
            <span class="chip" onclick="setAmt(10)">₹10</span>
            <span class="chip" onclick="setAmt(50)">₹50</span>
            <span class="chip" onclick="setAmt(100)">₹100</span>
        </div>

        <button class="btn-main" onclick="openQR()">⚡ Generate Instant QR</button>
    </div>

    <!-- Recent Payments -->
    <div class="card history-card">
        <div class="history-head">
            <h3>Recent Payments (সাম্প্রতিক লেনদেন)</h3>
            <span style="font-size: 11px; color: #2563eb; cursor: pointer;" onclick="loadTxns()">🔄 Refresh</span>
        </div>
        <table class="txn-table">
            <thead>
                <tr><th>Amount</th><th>প্রেরক / কার থেকে এলো</th><th>UTR</th><th>Time</th></tr>
            </thead>
            <tbody id="txnRows">
                <tr><td colspan="4" style="text-align: center; color: #94a3b8; padding: 12px;">লোড হচ্ছে...</td></tr>
            </tbody>
        </table>
    </div>
</div>

<!-- Modal -->
<div id="gatewayModal">
    <div class="modal-box">
        <div id="qrScreen">
            <span style="font-size: 11px; color: #64748b; font-weight: 700;">SCAN & PAY UPI</span>
            <div id="dispVal" style="font-size: 2.2rem; font-weight: 800; margin: 4px 0 6px;">₹0.00</div>
            <div class="qr-stage"><div id="qrcode"></div></div>
            <div style="font-size: 12px; color: #d97706; margin-bottom: 12px;" id="timer">⏳ Validity: 01:00</div>
            <button style="width: 100%; padding: 10px; background: #f1f5f9; color: #ef4444; border: none; border-radius: 12px; font-weight: 700; cursor: pointer;" onclick="closeModal()">বাতিল করুন</button>
        </div>

        <div id="succScreen" style="display: none;">
            <div style="width: 64px; height: 64px; background: #dcfce7; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 10px; font-size: 30px; color: #16a34a;">✓</div>
            <h3 style="color: #16a34a; font-weight: 800; margin-bottom: 2px;">পেমেন্ট সফল হয়েছে!</h3>
            <p style="font-size: 12px; color: #64748b;">Instant Bank Credit Verified</p>

            <div class="receipt-card">
                <div class="receipt-row"><span>পরিমাণ:</span><span id="recAmt" style="color:#16a34a;">₹0.00</span></div>
                <div class="receipt-row"><span>কার থেকে এলো:</span><span id="recSender" style="color:#2563eb; font-weight:800;">-</span></div>
                <div class="receipt-row"><span>প্রেরকের UPI:</span><span id="recSenderUpi" style="color:#0284c7; font-family:monospace;">-</span></div>
                <div class="receipt-row"><span>UTR নম্বর:</span><span id="recUtr" style="font-family:monospace;">-</span></div>
                <div class="receipt-row"><span>রিসিভার:</span><span id="recReceiver">-</span></div>
            </div>

            <button class="btn-main" onclick="closeModal()">নতুন পেমেন্ট নিন</button>
        </div>
    </div>
</div>

<script>
    let pollLoop = null, timerLoop = null, curOrder = "";

    window.onload = function() {
        checkBackendStatus();
        loadTxns();
        setInterval(loadTxns, 3000);
    };

    function setAmt(val) { document.getElementById('payAmt').value = val; }
    function toggleSettings() {
        const s = document.getElementById('settingsBox');
        s.style.display = s.style.display === 'block' ? 'none' : 'block';
    }

    async function checkBackendStatus() {
        try {
            const res = await fetch('/api/status?_t=' + Date.now());
            const d = await res.json();
            const badge = document.getElementById('connStatus');
            if(d.email_connected) {
                badge.className = "status-pill connected";
                badge.innerText = `🟢 Connected: ${d.email_user}`;
            } else {
                badge.className = "status-pill disconnected";
                badge.innerText = "🔴 Email Not Connected";
                toggleSettings();
            }
            if(d.upi_id) document.getElementById('upiInput').value = d.upi_id;
        } catch(e) {
            document.getElementById('connStatus').innerText = "🔴 Backend Offline";
        }
    }

    async function saveUpiId() {
        const upi_id = document.getElementById('upiInput').value.trim();
        if(!upi_id) return alert("UPI ID লিখুন!");
        const res = await fetch('/api/save-upi', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({upi_id})
        });
        const d = await res.json();
        if(d.success) {
            const st = document.getElementById('saveStatus');
            st.innerText = "✓ Saved!";
            setTimeout(() => st.innerText = "", 2500);
        } else {
            alert(d.message);
        }
    }

    async function saveEmail() {
        const email = document.getElementById('emailInput').value;
        const app_password = document.getElementById('passInput').value;
        const res = await fetch('/api/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({email, app_password})
        });
        const d = await res.json();
        alert(d.message);
        if(d.success) location.reload();
    }

    async function openQR() {
        const amt = document.getElementById('payAmt').value;
        if(!amt || parseFloat(amt) <= 0) return alert("সঠিক অ্যামাউন্ট লিখুন!");

        const res = await fetch('/api/create', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({amount: parseFloat(amt)})
        });
        const d = await res.json();
        if(!d.success) return alert(d.message);

        curOrder = d.order_id;
        document.getElementById('dispVal').innerText = `₹${parseFloat(d.amount).toFixed(2)}`;

        const upiStr = `upi://pay?pa=${d.upi_id}&pn=Merchant&am=${d.amount}&cu=INR&tn=${d.order_id}`;
        const box = document.getElementById('qrcode');
        box.innerHTML = "";
        try {
            new QRCode(box, {text: upiStr, width: 180, height: 180});
        } catch(e) {
            box.innerHTML = `<img src="https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=${encodeURIComponent(upiStr)}" width="180" height="180" />`;
        }

        document.getElementById('qrScreen').style.display = 'block';
        document.getElementById('succScreen').style.display = 'none';
        document.getElementById('gatewayModal').style.display = 'flex';

        let left = 60;
        clearInterval(timerLoop);
        timerLoop = setInterval(() => {
            left--;
            document.getElementById('timer').innerText = `⏳ Validity: 00:${left < 10 ? '0' : ''}${left}`;
            if(left <= 0) { closeModal(); alert("সময় শেষ!"); }
        }, 1000);

        // ⚡ ০.১ সেকেন্ড পোলিং
        clearInterval(pollLoop);
        pollLoop = setInterval(async () => {
            try {
                const cRes = await fetch(`/api/check?order_id=${curOrder}&_t=${Date.now()}`);
                const cData = await cRes.json();
                if(cData.status === "PAID") {
                    clearInterval(pollLoop);
                    clearInterval(timerLoop);

                    document.getElementById('recAmt').innerText = `₹${parseFloat(cData.amount).toFixed(2)}`;
                    document.getElementById('recSender').innerText = cData.sender_name;
                    document.getElementById('recSenderUpi').innerText = cData.sender_upi;
                    document.getElementById('recUtr').innerText = cData.utr;
                    document.getElementById('recReceiver').innerText = document.getElementById('upiInput').value;

                    document.getElementById('qrScreen').style.display = 'none';
                    document.getElementById('succScreen').style.display = 'block';

                    speakAlert(cData.sender_name, cData.amount);
                    loadTxns();
                }
            } catch(e) {}
        }, 100);
    }

    function closeModal() {
        clearInterval(pollLoop);
        clearInterval(timerLoop);
        document.getElementById('gatewayModal').style.display = 'none';
    }

    function speakAlert(name, amt) {
        if ('speechSynthesis' in window) {
            const senderText = name && name !== "Customer" ? `${name}-এর থেকে` : "";
            const msg = new SpeechSynthesisUtterance(`পেমেন্ট সফল হয়েছে! ${senderText} ${amt} টাকা জমা হয়েছে।`);
            msg.lang = 'bn-IN';
            window.speechSynthesis.speak(msg);
        }
    }

    async function loadTxns() {
        try {
            const res = await fetch('/api/transactions?_t=' + Date.now());
            const d = await res.json();
            const tbody = document.getElementById('txnRows');

            if(!d.transactions || d.transactions.length === 0) {
                tbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: #94a3b8; padding: 12px;">কোনো পেমেন্ট নেই...</td></tr>`;
                return;
            }

            let rows = '';
            d.transactions.forEach(t => {
                rows += `<tr>
                    <td class="txn-amt">₹${t.amount}</td>
                    <td>
                        <strong style="color: #0f172a;">${t.sender_name || 'Customer'}</strong><br>
                        <small style="color: #64748b; font-family: monospace;">${t.sender_upi || '-'}</small>
                    </td>
                    <td style="font-family: monospace;">${t.utr}</td>
                    <td style="color: #64748b;">${t.time}</td>
                </tr>`;
            });
            tbody.innerHTML = rows;
        } catch(e) {}
    }
</script>
</body>
</html>
"""

@app.route('/')
def home():
    return render_template_string(HTML_PAGE)

if __name__ == '__main__':
    print("\n=======================================================")
    print(" ⚡ Auto-Verification Engine Live: http://localhost:5000")
    print("=======================================================\n")
    app.run(host='0.0.0.0', port=5000, debug=False)
EOF
python app.py
