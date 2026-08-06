import imaplib
import email
import re
import os
import secrets
import psycopg2
from flask import Flask, request, jsonify, send_from_directory
from email.header import decode_header
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import threading
import time
import requests as req_lib

try:
    from dotenv import load_dotenv
    load_dotenv()
except:
    pass

app = Flask(__name__)

DB_HOST  = os.environ.get("DB_HOST", "")
DB_PORT  = os.environ.get("DB_PORT", "6543")
DB_NAME  = os.environ.get("DB_NAME", "postgres")
DB_USER  = os.environ.get("DB_USER", "")
DB_PASS  = os.environ.get("DB_PASS", "")
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
FAMPAY_SENDER = "no-reply@famapp.in"

def get_db():
    return psycopg2.connect(
        host=DB_HOST, port=int(DB_PORT), dbname=DB_NAME,
        user=DB_USER, password=DB_PASS,
        sslmode="require", connect_timeout=15
    )

def init_db():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_users (
                id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                api_key    TEXT UNIQUE NOT NULL,
                gmail_user TEXT NOT NULL,
                gmail_pass TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS used_utrs (
                id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                api_key  TEXT NOT NULL,
                utr      TEXT NOT NULL,
                amount   NUMERIC,
                used_at  TIMESTAMP DEFAULT NOW(),
                UNIQUE(api_key, utr)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS deleted_keys (
                id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                api_key    TEXT NOT NULL,
                gmail_user TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                deleted_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("✅ DB ready")
    except Exception as e:
        print(f"❌ DB init error: {e}")

def get_user_by_key(api_key):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT gmail_user, gmail_pass FROM api_users WHERE api_key = %s", (api_key,))
    row  = cur.fetchone()
    cur.close(); conn.close()
    return row

def is_utr_used(api_key, utr):
    conn  = get_db()
    cur   = conn.cursor()
    cur.execute("SELECT 1 FROM used_utrs WHERE api_key = %s AND utr = %s", (api_key, utr))
    found = cur.fetchone() is not None
    cur.close(); conn.close()
    return found

def save_utr(api_key, utr, amount):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("INSERT INTO used_utrs (api_key, utr, amount) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (api_key, utr, amount))
    conn.commit(); cur.close(); conn.close()

def verify_gmail(gmail_user, gmail_pass):
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(gmail_user, gmail_pass)
    m.logout()

def extract_text(msg):
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct      = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload: continue
            decoded = payload.decode("utf-8", errors="ignore")
            if ct == "text/plain":
                text += decoded + "\n"
            elif ct == "text/html" and not text.strip():
                soup  = BeautifulSoup(decoded, "html.parser")
                text += soup.get_text(separator=" ") + "\n"
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode("utf-8", errors="ignore")
            if msg.get_content_type() == "text/html":
                soup = BeautifulSoup(decoded, "html.parser")
                text = soup.get_text(separator=" ")
            else:
                text = decoded
    return text.replace('\xa0', ' ').replace('\u200b', '')


def parse_email(text):
    text = re.sub(r'\s+', ' ', text)

    # ── Amount ──
    amount = None
    for pat in [
        r'(?:₹|Rs\.?\s*|INR\s*)(\d{1,6}(?:\.\d{1,2})?)',
        r'(\d{1,6}(?:\.\d{1,2})?)\s*(?:₹|Rs\.?|INR)',
        r'(?:amount|paid|payment\s+of|debited|received)[^\d]{0,15}(\d{1,6}(?:\.\d{1,2})?)',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try: amount = float(m.group(1).replace(',', '')); break
            except: continue

    # ── UTR — Other UPI → FamPay (numeric only) ──
    utr = None
    for pat in [
        r'UTR\s*(?:No\.?|Number|ID|#|:)?\s*[:\-]?\s*(\d{10,22})',
        r'UPI\s+Ref(?:erence)?\s*[:\-]?\s*(\d{10,22})',
        r'\bUTR[:\s]+(\d{10,22})',
        r'\b(\d{12})\b',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m: utr = m.group(1).strip(); break

    # ── TXN ID — FamPay → FamPay (starts with FMP) ──
    txn_id = None
    for pat in [
        r'transaction\s+id\s+(FMP[A-Z0-9]{5,20})',
        r'\b(FMP[A-Z0-9]{5,20})\b',
        r'txn\s*(?:id)?\s*[:\-]?\s*(FMP[A-Z0-9]{5,20})',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m: txn_id = m.group(1).strip().upper(); break

    return amount, utr, txn_id

def fetch_emails(gmail_user, gmail_pass, days=3):
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(gmail_user, gmail_pass)
    mail.select("INBOX")
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    _, nums = mail.search(None, f'(FROM "{FAMPAY_SENDER}" SINCE {since})')
    results = []
    for num in nums[0].split():
        try:
            _, msg_data = mail.fetch(num, "(RFC822)")
            msg      = email.message_from_bytes(msg_data[0][1])
            sender   = msg.get("From", "")
            subj_raw = msg.get("Subject", "")
            subject  = ""
            for part, enc in decode_header(subj_raw):
                if isinstance(part, bytes): subject += part.decode(enc or "utf-8", errors="ignore")
                else: subject += part
            text = extract_text(msg)
            amount, utr, txn_id = parse_email(text)
            results.append({
                "sender": sender, "subject": subject,
                "date": msg.get("Date", ""),
                "amount": amount, "utr": utr, "txn_id": txn_id,
                "preview": text[:300].strip()
            })
        except Exception as e:
            results.append({"error": str(e)})
    mail.logout()
    return results

# ── Routes ────────────────────────────────────────────────

@app.route("/")
def index():
    return jsonify({
        "status": "running 🚀",
        "service": "FamPay Payment Verification API",
        "by": "@growthcentre",
        "support": "@growthcentre"
    })

@app.route("/web")
def website():
    return send_from_directory(".", "index.html")
@app.route("/health")
def health():
    try:
        conn = get_db(); conn.close(); db_ok = True
    except Exception as e:
        db_ok = str(e)
    return jsonify({"status": "ok", "db": db_ok})

@app.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "JSON body required"}), 400
    gmail_user = str(data.get("gmail_user", "")).strip().lower()
    gmail_pass = str(data.get("gmail_pass", "")).replace('\xa0','').replace(' ','').strip()
    if not gmail_user or not gmail_pass or "@" not in gmail_user:
        return jsonify({"success": False, "error": "Valid Gmail aur App Password do"}), 400
    try:
        verify_gmail(gmail_user, gmail_pass)
    except Exception as e:
        return jsonify({"success": False, "error": f"Gmail login failed: {str(e)}"}), 400
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT api_key, created_at FROM api_users WHERE gmail_user = %s", (gmail_user,))
        existing = cur.fetchone()
        if existing:
            cur.close(); conn.close()
            return jsonify({"success": True, "api_key": existing[0],
                           "created_at": existing[1].strftime("%d %b %Y, %I:%M %p"),
                           "message": "Already registered"})
        api_key = "fmpay_" + secrets.token_hex(20)
        cur.execute("INSERT INTO api_users (api_key, gmail_user, gmail_pass) VALUES (%s, %s, %s)",
                    (api_key, gmail_user, gmail_pass))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "api_key": api_key,
                       "created_at": datetime.now().strftime("%d %b %Y, %I:%M %p")})
    except Exception as e:
        return jsonify({"success": False, "error": f"DB error: {str(e)}"}), 500

@app.route("/my-key", methods=["POST"])
def my_key():
    """Gmail se current key info + deleted history dekho"""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "JSON required"}), 400
    gmail_user = str(data.get("gmail_user", "")).strip().lower()
    gmail_pass = str(data.get("gmail_pass", "")).replace('\xa0','').replace(' ','').strip()
    if not gmail_user or not gmail_pass:
        return jsonify({"success": False, "error": "Gmail aur password do"}), 400
    try:
        verify_gmail(gmail_user, gmail_pass)
    except Exception as e:
        return jsonify({"success": False, "error": f"Gmail login failed: {str(e)}"}), 400
    try:
        conn = get_db(); cur = conn.cursor()
        # Active key
        cur.execute("SELECT api_key, created_at FROM api_users WHERE gmail_user = %s", (gmail_user,))
        active = cur.fetchone()
        # Deleted keys history
        cur.execute("SELECT api_key, created_at, deleted_at FROM deleted_keys WHERE gmail_user = %s ORDER BY deleted_at DESC",
                    (gmail_user,))
        deleted = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({
            "success": True,
            "active_key": {
                "api_key": active[0],
                "created_at": active[1].strftime("%d %b %Y, %I:%M %p")
            } if active else None,
            "deleted_keys": [
                {
                    "api_key": row[0],
                    "created_at": row[1].strftime("%d %b %Y, %I:%M %p"),
                    "deleted_at": row[2].strftime("%d %b %Y, %I:%M %p")
                }
                for row in deleted
            ]
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/delete-key", methods=["POST"])
def delete_key():
    """API key delete karo"""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "JSON required"}), 400
    gmail_user = str(data.get("gmail_user", "")).strip().lower()
    gmail_pass = str(data.get("gmail_pass", "")).replace('\xa0','').replace(' ','').strip()
    if not gmail_user or not gmail_pass:
        return jsonify({"success": False, "error": "Gmail aur password do"}), 400
    try:
        verify_gmail(gmail_user, gmail_pass)
    except Exception as e:
        return jsonify({"success": False, "error": f"Gmail login failed: {str(e)}"}), 400
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT api_key, created_at FROM api_users WHERE gmail_user = %s", (gmail_user,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"success": False, "error": "Koi active key nahi mili"}), 404
        api_key, created_at = row
        # Deleted table me save karo
        cur.execute("INSERT INTO deleted_keys (api_key, gmail_user, created_at) VALUES (%s, %s, %s)",
                    (api_key, gmail_user, created_at))
        # Active table se delete karo
        cur.execute("DELETE FROM api_users WHERE gmail_user = %s", (gmail_user,))
        # UTRs bhi clear karo
        cur.execute("DELETE FROM used_utrs WHERE api_key = %s", (api_key,))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "message": "API key delete ho gaya!", "deleted_key": api_key})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/verify", methods=["POST"])
def verify():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "JSON required"}), 400

    api_key    = str(data.get("api_key", "")).strip()
    req_id     = str(data.get("utr", "")).strip().upper()   # UTR ya TXN ID dono accept
    req_amount = data.get("amount")

    if not api_key:
        return jsonify({"success": False, "error": "api_key required"}), 400
    if not req_id or req_amount is None:
        return jsonify({"success": False, "error": "utr/txn_id aur amount required"}), 400
    try:
        req_amount = float(req_amount)
    except:
        return jsonify({"success": False, "error": "Invalid amount"}), 400

    try:
        creds = get_user_by_key(api_key)
    except Exception as e:
        return jsonify({"success": False, "error": f"DB error: {str(e)}"}), 500

    if not creds:
        return jsonify({"success": False, "error": "Invalid API key"}), 401

    # Duplicate check
    try:
        if is_utr_used(api_key, req_id):
            return jsonify({"success": True, "verified": False, "error": "UTR already used! Duplicate payment."})
    except Exception as e:
        return jsonify({"success": False, "error": f"DB error: {str(e)}"}), 500

    # Gmail fetch
    try:
        emails = fetch_emails(creds[0], creds[1], int(data.get("days", 3)))
    except Exception as e:
        return jsonify({"success": False, "error": f"Gmail error: {str(e)}"}), 500

    for item in emails:
        if "error" in item:
            continue

        sender_ok = FAMPAY_SENDER.lower() in item["sender"].lower()
        amount_ok = item["amount"] is not None and abs(item["amount"] - req_amount) < 0.5

        # UTR match (other UPI → FamPay)
        utr_ok = item["utr"] and item["utr"].upper() == req_id
        # TXN ID match (FamPay → FamPay)
        txn_ok = item["txn_id"] and item["txn_id"].upper() == req_id

        if sender_ok and amount_ok and (utr_ok or txn_ok):
            matched_id   = req_id
            payment_type = "FamPay→FamPay" if txn_ok else "UPI→FamPay"
            try:
                save_utr(api_key, matched_id, req_amount)
            except:
                pass
            return jsonify({
                "success": True,
                "verified": True,
                "utr": matched_id,
                "payment_type": payment_type,
                "amount": item["amount"],
                "subject": item["subject"],
                "date": item["date"]
            })

    return jsonify({
        "success": True,
        "verified": False,
        "message": "Transaction nahi mila",
        "emails_checked": len([e for e in emails if "error" not in e])
    })

def keep_alive():
    while True:
        time.sleep(600)
        if SELF_URL:
            try: req_lib.get(f"{SELF_URL}/health", timeout=10)
            except: pass

if __name__ == "__main__":
    init_db()
    if SELF_URL:
        threading.Thread(target=keep_alive, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🚀 FamPay SaaS API — port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
