import os
import re
import imaplib
import email
from email.header import decode_header
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# --- কনফিগারেশন (সরাসরি কোডে যুক্ত) ---
UPI_ID = "tanmaykundu439@oksbi"  # আপনার UPI আইডি প্রয়োজন অনুযায়ী বদলাতে পারেন
MERCHANT_NAME = "Fuel Station Gateway"

GMAIL_USER = "tanmaykundu439@gmail.com"
GMAIL_PASS = "cjmjsbslgwdzbfrw"  # স্পেস ছাড়া অ্যাপ পাসওয়ার্ড

# --- জিমেইল থেকে পেমেন্ট চেক করার ফাংশন ---
def check_payment_in_gmail(expected_amount):
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_PASS)
        mail.select("inbox")

        # আনরিড ও সাম্প্রতিক ইমেইল সার্চ
        status, messages = mail.search(None, '(UNSEEN)')
        mail_ids = messages[0].split()

        if not mail_ids:
            # আনরিড না পেলে শেষ ৫টি ইমেইল চেক করবে
            status, messages = mail.search(None, 'ALL')
            mail_ids = messages[0].split()[-5:]

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

                    # ইমেইলে টাকা জমার পরিমাণ ও ক্রেডিট মেসেজ খোঁজা
                    body_lower = body.lower()
                    if ("credited" in body_lower or "received" in body_lower or "upi" in body_lower):
                        amounts = re.findall(r'(?:rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)', body, re.IGNORECASE)
                        for amt in amounts:
                            clean_amt = float(amt.replace(',', ''))
                            if float(expected_amount) == clean_amt:
                                mail.logout()
                                return True, "Payment Verified Successfully"

        mail.logout()
        return False, "Payment not received yet"
    except Exception as e:
        return False, f"Error: {str(e)}"

# --- হোম পেজ UI (HTML + কিউআর জেনারেটর + অটো চেকার) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>UPI Payment Gateway</title>
    <script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0f172a; color: #fff; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 15px; }
        .card { background: #1e293b; border-radius: 16px; padding: 25px; width: 100%; max-width: 380px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); text-align: center; }
        h2 { margin-bottom: 20px; color: #38bdf8; }
        input { width: 90%; padding: 12px; margin-bottom: 15px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #fff; font-size: 16px; text-align: center; }
        button { background: #0284c7; color: #fff; border: none; padding: 12px 20px; border-radius: 8px; font-size: 16px; cursor: pointer; width: 96%; font-weight: bold; }
        button:hover { background: #0369a1; }
        #qr-container { margin: 20px auto; background: #fff; padding: 12px; border-radius: 12px; display: inline-block; }
        #status-box { margin-top: 15px; font-weight: bold; font-size: 15px; }
        .waiting { color: #facc15; }
        .success { color: #4ade80; font-size: 20px; }
        .pulse { animation: pulse 1.5s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
    </style>
</head>
<body>

<div class="card">
    <h2>⛽ Fuel Payment</h2>
    
    <div id="input-section">
        <input type="number" id="amount" placeholder="টাকার পরিমাণ লিখুন (যেমন: 500)" min="1">
        <button onclick="startPayment()">QR তৈরি করুন</button>
    </div>

    <div id="payment-section" style="display:none;">
        <h3 id="display-amount" style="margin: 10px 0;"></h3>
        <div id="qr-container"></div>
        <div id="status-box" class="waiting pulse">পেমেন্টের অপেক্ষা করা হচ্ছে...</div>
    </div>
</div>

<script>
    let checkInterval = null;

    function startPayment() {
        const amt = document.getElementById('amount').value;
        if (!amt || amt <= 0) {
            alert("সঠিক অ্যামাউন্ট লিখুন!");
            return;
        }

        document.getElementById('input-section').style.display = 'none';
        document.getElementById('payment-section').style.display = 'block';
        document.getElementById('display-amount').innerText = "প্রদেয় পরিমাণ: ₹" + amt;

        // Dynamic UPI QR String
        const upiUrl = `upi://pay?pa={{ upi_id }}&pn={{ merchant_name }}&am=${amt}&cu=INR`;

        // Generate QR
        document.getElementById('qr-container').innerHTML = "";
        new QRCode(document.getElementById("qr-container"), {
            text: upiUrl,
            width: 200,
            height: 200
        });

        // Auto Poll Gmail every 5 seconds
        checkInterval = setInterval(() => {
            fetch(`/check_status?amount=${amt}`)
                .then(res => res.json())
                .then(data => {
                    if (data.status === "success") {
                        clearInterval(checkInterval);
                        document.getElementById('status-box').className = "success";
                        document.getElementById('status-box').innerHTML = "✅ Payment Successful!";
                        document.getElementById('qr-container').style.display = "none";
                    }
                })
                .catch(err => console.log(err));
        }, 5000);
    }
</script>

</body>
</html>
"""

# --- রাউটসমূহ ---
@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE, upi_id=UPI_ID, merchant_name=MERCHANT_NAME)

@app.route('/create_qr', methods=['GET'])
def create_qr():
    amt = request.args.get('amount', '0')
    upi_link = f"upi://pay?pa={UPI_ID}&pn={MERCHANT_NAME}&am={amt}&cu=INR"
    return jsonify({
        "status": "success",
        "amount": amt,
        "upi_url": upi_link
    })

@app.route('/check_status', methods=['GET'])
def check_status():
    amount = request.args.get('amount')
    if not amount:
        return jsonify({"status": "failed", "message": "Amount is required"}), 400

    verified, msg = check_payment_in_gmail(amount)
    if verified:
        return jsonify({"status": "success", "message": msg})
    else:
        return jsonify({"status": "waiting", "message": msg})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
