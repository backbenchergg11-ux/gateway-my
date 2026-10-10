import os
import re
import imaplib
import email
from email.header import decode_header
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- কনফিগারেশন ---
# আপনার পাম্পের UPI আইডি ও নাম
UPI_ID = os.environ.get("UPI_ID", "your-vpa@upi")
MERCHANT_NAME = os.environ.get("MERCHANT_NAME", "Pump Station")

# Gmail IMAP কনফিগারেশন (Render-এর Environment Variables থেকে নেবে)
GMAIL_USER = os.environ.get("GMAIL_USER", "your-email@gmail.com")
GMAIL_PASS = os.environ.get("GMAIL_PASS", "your-app-password")  # 16-digit Google App Password
IMAP_SERVER = "imap.gmail.com"

# --- সাহায্যকারী ফাংশন: জিমেইল থেকে ট্রানজেকশন যাচাই ---
def verify_payment_via_email(expected_amount, expected_utr=None):
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(GMAIL_USER, GMAIL_PASS)
        mail.select("inbox")

        # সাম্প্রতিক আনরিড বা সব পেমেন্ট ইমেইল খোঁজা
        status, messages = mail.search(None, '(OR FROM "alerts" SUBJECT "credited")')
        if status != "OK" or not messages[0]:
            mail.logout()
            return {"status": "pending", "message": "কোনো ক্রেডিট অ্যালার্ট ইমেইল পাওয়া যায়নি"}

        email_ids = messages[0].split()
        # সবচেয়ে শেষের (সাম্প্রতিক) ৫টি ইমেইল চেক করা
        for e_id in reversed(email_ids[-5:]):
            res, msg_data = mail.fetch(e_id, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            content_type = part.get_content_type()
                            if content_type in ["text/plain", "text/html"]:
                                try:
                                    body += part.get_payload(decode=True).decode()
                                except:
                                    pass
                    else:
                        body = msg.get_payload(decode=True).decode()

                    # টাকার পরিমাণ মেলাচ্ছে কি না চেক করা (যেমন: Rs 500 বা INR 500)
                    amount_pattern = rf"(?:Rs\.?|INR|₹)\s*({expected_amount}(?:\.00)?)"
                    amount_match = re.search(amount_pattern, body, re.IGNORECASE)

                    # UTR / Ref No থাকলে চেক করা
                    utr_found = None
                    if expected_utr:
                        if expected_utr in body:
                            utr_found = expected_utr
                    else:
                        utr_match = re.search(r"\b\d{12}\b", body) # সাধারণ ১২ সংখ্যার UPI রেফারেন্স
                        if utr_match:
                            utr_found = utr_match.group(0)

                    if amount_match:
                        mail.logout()
                        return {
                            "status": "success",
                            "amount": expected_amount,
                            "utr": utr_found,
                            "message": "Payment verified successfully from email alert"
                        }

        mail.logout()
        return {"status": "pending", "message": "টাকার পরিমাণ অনুযায়ী কোনো লেনদেন মেলেনি"}

    except Exception as e:
        return {"status": "error", "message": str(e)}

# --- এন্ডপয়েন্ট ১: হোম রুট ---
@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "service": "Pump Payment Gateway & Verifier",
        "status": "online"
    }), 200

# --- এন্ডপয়েন্ট ২: পাম্পের ডায়নামিক UPI QR কোড তৈরি ---
@app.route('/create_qr', methods=['GET', 'POST'])
def create_qr():
    data = request.get_json(silent=True) or request.args
    amount = data.get("amount")
    note = data.get("note", "Fuel Payment")

    if not amount:
        return jsonify({"status": "error", "message": "Amount is required"}), 400

    # স্ট্যান্ডার্ড UPI স্ট্রিং
    upi_payload = f"upi://pay?pa={UPI_ID}&pn={MERCHANT_NAME}&am={amount}&cu=INR&tn={note}"
    
    # অটোমেটিক স্ক্যানযোগ্য QR ইমেজ লিঙ্ক
    qr_image_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={upi_payload}"

    return jsonify({
        "status": "success",
        "amount": amount,
        "upi_url": upi_payload,
        "qr_image_url": qr_image_url
    }), 200

# --- এন্ডপয়েন্ট ৩: ইমেইল চেক করে ট্রানজেকশন ভেরিফাই করা ---
@app.route('/check_status', methods=['POST', 'GET'])
def check_status():
    data = request.get_json(silent=True) or request.args
    amount = data.get("amount")
    utr = data.get("utr")

    if not amount:
        return jsonify({"status": "error", "message": "Amount is required for checking"}), 400

    result = verify_payment_via_email(expected_amount=amount, expected_utr=utr)
    return jsonify(result), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
