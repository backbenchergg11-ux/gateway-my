import os
import re
import uuid
import random
import imaplib
import email
from email.header import decode_header
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# --- কনফিগারেশন ---
CONFIG = {
    "GMAIL_USER": "tanmaykundu439@gmail.com",
    "GMAIL_PASS": "cjmjsbslgwdzbfrw",
    "UPI_ID": "tanmaykundu439@yesfam",
    "MERCHANT_NAME": "Fuel Station POS"
}

PAYMENT_HISTORY = []
ACTIVE_ORDERS = {}

# --- জিমেইল কানেকশন টেস্ট ---
def test_gmail_connection():
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(CONFIG["GMAIL_USER"], CONFIG["GMAIL_PASS"])
        mail.logout()
        return True, "Gmail Connected & Active"
    except Exception as e:
        return False, f"Gmail Connection Error: {str(e)}"

# --- জিমেইল থেকে ইউনিক সিক্রেট কোড এবং অ্যামাউন্ট দুটোই মেলানো ---
def verify_payment_mail(secret_code, expected_amount):
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(CONFIG["GMAIL_USER"], CONFIG["GMAIL_PASS"])
        mail.select("inbox")

        # আনরিড ও শেষ ১৫টি ইমেইল সার্চ করবে
        status, messages = mail.search(None, '(UNSEEN)')
        mail_ids = messages[0].split()
        if not mail_ids:
            status, messages = mail.search(None, 'ALL')
            mail_ids = messages[0].split()[-15:]

        target_code = str(secret_code).strip().lower()

        for m_id in reversed(mail_ids):
            status, data = mail.fetch(m_id, '(RFC822)')
            for response_part in data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body += part.get_payload(decode=True).decode(errors='ignore')
                    else:
                        body = msg.get_payload(decode=True).decode(errors='ignore')

                    body_clean = body.lower()

                    # ১. সিক্রেট কোড অবশ্যই ইমেইলের মেসেজে থাকতে হবে
                    if target_code in body_clean:
                        # ২. ক্রেডিট হওয়া নিশ্চিত করা
                        if any(w in body_clean for w in ["credited", "received", "deposited", "upi"]):
                            amounts = re.findall(r'(?:rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)', body, re.IGNORECASE)
                            for amt in amounts:
                                clean_amt = float(amt.replace(',', ''))
                                # ৩. অ্যামাউন্ট ও কোড দুটোই ম্যাচ করলে তবেই SUCCESS
                                if float(expected_amount) == clean_amt:
                                    utr_match = re.search(r'(?:ref(?:\s*no)?|rrn|utr)[:\s]*([0-9]{10,14})', body, re.IGNORECASE)
                                    utr = utr_match.group(1) if utr_match else "UPI-" + secret_code
                                    mail.logout()
                                    return True, utr

        mail.logout()
        return False, None
    except Exception as e:
        return False, None

# --- Merchant UI ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Fuel POS Merchant Terminal</title>
    <script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
    <style>
        :root {
            --bg-color: #f1f5f9;
            --card-bg: #ffffff;
            --primary: #2563eb;
            --primary-hover: #1d4ed8;
            --success: #16a34a;
            --danger: #dc2626;
            --text-dark: #0f172a;
            --text-muted: #64748b;
            --border-color: #e2e8f0;
        }

        body {
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-dark);
            margin: 0;
            padding: 24px 16px;
        }

        .container {
            max-width: 950px;
            margin: 0 auto;
            display: flex;
            flex-direction: column;
            gap: 20px;
        }

        .header-card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 20px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 15px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
        }

        .merchant-title {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .merchant-logo {
            width: 48px;
            height: 48px;
            background: #eff6ff;
            color: var(--primary);
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 24px;
            border: 1px solid #bfdbfe;
        }

        .badge-group {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
        }

        .badge {
            padding: 6px 14px;
            border-radius: 9999px;
            font-size: 13px;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }

        .badge-discount {
            background: #fef3c7;
            color: #d97706;
            border: 1px solid #fde68a;
        }

        .badge-online {
            background: #dcfce7;
            color: var(--success);
            border: 1px solid #bbf7d0;
        }

        .badge-offline {
            background: #fee2e2;
            color: var(--danger);
            border: 1px solid #fecaca;
        }

        .grid-layout {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }

        @media (max-width: 768px) {
            .grid-layout { grid-template-columns: 1fr; }
        }

        .card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 24px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
        }

        h3 {
            margin-top: 0;
            font-size: 18px;
            color: var(--text-dark);
            margin-bottom: 16px;
        }

        label {
            font-size: 13px;
            font-weight: 600;
            color: var(--text-muted);
            display: block;
            margin-bottom: 6px;
        }

        input {
            width: 100%;
            box-sizing: border-box;
            padding: 12px 14px;
            border-radius: 10px;
            border: 1px solid var(--border-color);
            background: #f8fafc;
            color: var(--text-dark);
            font-size: 15px;
            margin-bottom: 14px;
            outline: none;
        }

        input:focus {
            border-color: var(--primary);
            background: #ffffff;
        }

        button {
            background: var(--primary);
            color: #ffffff;
            border: none;
            padding: 13px 20px;
            border-radius: 10px;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            width: 100%;
            transition: 0.2s;
        }

        button:hover { background: var(--primary-hover); }
        button:disabled { background: #94a3b8; cursor: not-allowed; }

        .qr-display { text-align: center; }

        #qr-box {
            padding: 14px;
            background: #ffffff;
            border: 2px dashed #cbd5e1;
            border-radius: 14px;
            display: inline-block;
            margin: 14px auto;
        }

        .status-pill {
            font-size: 15px;
            font-weight: 600;
            padding: 10px;
            border-radius: 8px;
            margin-top: 10px;
        }

        .status-waiting {
            background: #fef9c3;
            color: #854d0e;
            animation: pulse 1.5s infinite;
        }

        .status-success {
            background: #dcfce7;
            color: #15803d;
            font-size: 17px;
        }

        .code-tag {
            background: #e0f2fe;
            color: #0369a1;
            padding: 4px 8px;
            border-radius: 6px;
            font-family: monospace;
            font-weight: bold;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.6; }
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
            margin-top: 10px;
        }

        th, td {
            padding: 12px 14px;
            text-align: left;
            border-bottom: 1px solid var(--border-color);
        }

        th { background: #f8fafc; color: var(--text-muted); font-weight: 600; }
        tr:hover { background: #f8fafc; }
    </style>
</head>
<body>

<div class="container">
    <div class="header-card">
        <div class="merchant-title">
            <div class="merchant-logo">⛽</div>
            <div>
                <h2 style="margin: 0; font-size: 20px;">Fuel POS Terminal</h2>
                <div style="font-size: 13px; color: var(--text-muted); margin-top: 2px;">Strict Code Verification Engine</div>
            </div>
        </div>

        <div class="badge-group">
            <div class="badge badge-discount">
                <span>🏷️</span> Instant 0.75% UPI Rebate
            </div>
            <div id="email-status-badge" class="badge badge-offline">
                <span>●</span> Checking Gmail...
            </div>
        </div>
    </div>

    <div class="grid-layout">
        <div class="card">
            <h3>⚙️ Merchant Configuration</h3>
            <label>Active UPI ID</label>
            <input type="text" id="upi-input" value="{{ config.UPI_ID }}">
            <label>Merchant / Pump Name</label>
            <input type="text" id="merchant-input" value="{{ config.MERCHANT_NAME }}">
            <button onclick="updateSettings()" style="background: #334155;">Save Settings</button>
        </div>

        <div class="card qr-display">
            <h3 style="text-align: left;">💳 Generate Dynamic Payment QR</h3>
            
            <div id="create-view">
                <input type="number" id="pay-amount" placeholder="টাকার পরিমাণ লিখুন (যেমন: 500)" min="1">
                <button id="gen-btn" onclick="generateQR()" disabled>Generate QR Code</button>
                <div id="lock-msg" style="color: var(--danger); font-size: 13px; margin-top: 10px; display: none;">
                    ⚠️ Gmail disconnected! QR generation locked.
                </div>
            </div>

            <div id="payment-view" style="display: none;">
                <div id="order-info" style="font-weight: bold; color: var(--primary); font-size: 16px;"></div>
                <div style="margin-top: 6px; font-size: 13px; color: var(--text-muted);">
                    UPI Bio Code: <span id="secret-code-display" class="code-tag"></span>
                </div>
                <div id="qr-box"></div>
                <div id="payment-status" class="status-pill status-waiting">Waiting for exact bank email...</div>
                <button onclick="resetQRView()" style="margin-top: 15px; background: #64748b;">New Bill</button>
            </div>
        </div>
    </div>

    <div class="card">
        <h3>📋 Live Transaction History</h3>
        <table>
            <thead>
                <tr>
                    <th>Order ID</th>
                    <th>Code</th>
                    <th>Amount</th>
                    <th>Status</th>
                    <th>UTR / Ref</th>
                    <th>Time</th>
                </tr>
            </thead>
            <tbody id="history-body">
                <tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 20px;">No verified transactions yet</td></tr>
            </tbody>
        </table>
    </div>
</div>

<script>
    let pollInterval = null;
    let currentOrderId = null;

    function checkEmailStatus() {
        fetch('/api/check_email_status')
            .then(res => res.json())
            .then(data => {
                const badge = document.getElementById('email-status-badge');
                const genBtn = document.getElementById('gen-btn');
                const lockMsg = document.getElementById('lock-msg');

                if (data.connected) {
                    badge.className = 'badge badge-online';
                    badge.innerHTML = '● Gmail Connected';
                    genBtn.disabled = false;
                    lockMsg.style.display = 'none';
                } else {
                    badge.className = 'badge badge-offline';
                    badge.innerHTML = '● Gmail Offline';
                    genBtn.disabled = true;
                    lockMsg.style.display = 'block';
                    lockMsg.innerText = '⚠️ ' + data.message;
                }
            })
            .catch(() => {
                const badge = document.getElementById('email-status-badge');
                badge.className = 'badge badge-offline';
                badge.innerHTML = '● Server Error';
            });
    }

    function updateSettings() {
        const upi = document.getElementById('upi-input').value;
        const name = document.getElementById('merchant-input').value;
        fetch('/api/update_settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ upi_id: upi, merchant_name: name })
        }).then(res => res.json()).then(data => alert(data.message));
    }

    function generateQR() {
        const amt = document.getElementById('pay-amount').value;
        if (!amt || amt <= 0) return alert('সঠিক অ্যামাউন্ট লিখুন!');

        fetch(`/api/create_order?amount=${amt}`)
            .then(res => res.json())
            .then(data => {
                currentOrderId = data.order_id;
                document.getElementById('create-view').style.display = 'none';
                document.getElementById('payment-view').style.display = 'block';
                document.getElementById('order-info').innerText = `Total: ₹${amt}`;
                document.getElementById('secret-code-display').innerText = data.secret_code;

                document.getElementById('qr-box').innerHTML = '';
                new QRCode(document.getElementById('qr-box'), {
                    text: data.upi_url,
                    width: 200,
                    height: 200
                });

                startPolling(currentOrderId, data.secret_code, amt);
            });
    }

    function startPolling(orderId, secretCode, amt) {
        if (pollInterval) clearInterval(pollInterval);

        pollInterval = setInterval(() => {
            fetch(`/api/verify_order?order_id=${orderId}&secret_code=${secretCode}&amount=${amt}`)
                .then(res => res.json())
                .then(data => {
                    if (data.status === 'success') {
                        clearInterval(pollInterval);
                        const statusBox = document.getElementById('payment-status');
                        statusBox.className = 'status-pill status-success';
                        statusBox.innerHTML = `✅ Payment Verified (UTR: ${data.utr})`;
                        loadHistory();
                    }
                });
        }, 3000);
    }

    function loadHistory() {
        fetch('/api/history')
            .then(res => res.json())
            .then(list => {
                const tbody = document.getElementById('history-body');
                if (list.length === 0) return;
                tbody.innerHTML = list.map(item => `
                    <tr>
                        <td><strong>${item.order_id}</strong></td>
                        <td><span class="code-tag">${item.secret_code}</span></td>
                        <td>₹${item.amount}</td>
                        <td><span style="color: var(--success); font-weight: 700;">${item.status}</span></td>
                        <td>${item.utr}</td>
                        <td>${item.time}</td>
                    </tr>
                `).reverse().join('');
            });
    }

    function resetQRView() {
        if (pollInterval) clearInterval(pollInterval);
        document.getElementById('create-view').style.display = 'block';
        document.getElementById('payment-view').style.display = 'none';
        document.getElementById('payment-status').className = 'status-pill status-waiting';
        document.getElementById('payment-status').innerText = 'Waiting for exact bank email...';
    }

    checkEmailStatus();
    loadHistory();
    setInterval(checkEmailStatus, 30000);
</script>

</body>
</html>
"""

# --- API রাউটসমূহ ---
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
    if "upi_id" in data: CONFIG["UPI_ID"] = data["upi_id"].strip()
    if "merchant_name" in data: CONFIG["MERCHANT_NAME"] = data["merchant_name"].strip()
    return jsonify({"status": "success", "message": "UPI Settings updated successfully!"})

@app.route('/api/create_order')
def api_create_order():
    ok, _ = test_gmail_connection()
    if not ok:
        return jsonify({"status": "error", "message": "Gmail is disconnected"}), 400

    amt = request.args.get('amount', '0')
    # র্যান্ডম সিক্রেট কোড তৈরি (যেমন: FP8472) যা UPI নোটে পাঠানো হবে
    secret_code = "FP" + str(random.randint(1000, 9999))
    order_id = "ORD" + uuid.uuid4().hex[:6].upper()

    # tn (Transaction Note / Bio) হিসেবে secret_code যুক্ত করা
    upi_url = f"upi://pay?pa={CONFIG['UPI_ID']}&pn={CONFIG['MERCHANT_NAME']}&am={amt}&cu=INR&tn={secret_code}&tr={secret_code}"
    
    ACTIVE_ORDERS[order_id] = {
        "amount": amt,
        "secret_code": secret_code,
        "status": "PENDING"
    }
    return jsonify({
        "order_id": order_id,
        "secret_code": secret_code,
        "amount": amt,
        "upi_url": upi_url
    })

@app.route('/api/verify_order')
def api_verify_order():
    order_id = request.args.get('order_id')
    secret_code = request.args.get('secret_code')
    amount = request.args.get('amount')

    if order_id in ACTIVE_ORDERS and ACTIVE_ORDERS[order_id]["status"] == "SUCCESS":
        return jsonify({"status": "success", "utr": ACTIVE_ORDERS[order_id]["utr"]})

    # কোড ও অ্যামাউন্ট উভয়টি ইমেইলে যাচাই
    verified, utr = verify_payment_mail(secret_code, amount)
    if verified:
        import datetime
        record = {
            "order_id": order_id,
            "secret_code": secret_code,
            "amount": amount,
            "status": "SUCCESS",
            "utr": utr,
            "time": datetime.datetime.now().strftime("%I:%M:%S %p")
        }
        ACTIVE_ORDERS[order_id]["status"] = "SUCCESS"
        ACTIVE_ORDERS[order_id]["utr"] = utr
        PAYMENT_HISTORY.append(record)
        return jsonify({"status": "success", "utr": utr})
    
    return jsonify({"status": "waiting"})

@app.route('/api/history')
def api_history():
    return jsonify(PAYMENT_HISTORY)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
