# server.py
# Requires: pip install flask flask-cors
# Assumes: C++ SMTP server running on 127.0.0.1:2525
#          and saving .eml files into the MAIL_SPOOL_DIR directory.

import os
import glob
import socket
import uuid
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS

from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from email import policy
from email.parser import BytesParser

# --- Configuration (Must match C++ Server) ---
SMTP_SERVER_HOST = '127.0.0.1'
SMTP_SERVER_PORT = 2525
MAIL_SPOOL_DIR = 'mail_spool'
APP_DOMAIN = 'mydomain.com'

app = Flask(__name__)
CORS(app)

# --- Conceptual USER DATABASE (in-memory only) ---
# Stores: { user_email: { "id": uuid, "password": "HASH_xxx" } }
users = {}
# Stores: { session_token: user_email }
sessions = {}


# ========== Utility Auth Functions ==========

def conceptual_hash(password: str) -> str:
    """Very fake hash. Replace with bcrypt/argon2 in real app."""
    return f"HASH_{password}_SALT"


def conceptual_verify(email: str, password: str) -> bool:
    if email in users and users[email]['password'] == conceptual_hash(password):
        return True
    return False


def generate_session_token(email: str) -> str:
    token = str(uuid.uuid4())
    sessions[token] = email
    return token


def get_user_from_token(token: str):
    return sessions.get(token)


# ========== SMTP Client Logic ==========

def send_smtp_message(mail_from: str, rcpt_to_list, raw_message: str) -> bool:
    """
    Connects to the C++ SMTP server and executes the full protocol sequence.
    Sends raw_message as the DATA block (RFC 5322 email).
    """
    s = None
    try:
        s = socket.create_connection((SMTP_SERVER_HOST, SMTP_SERVER_PORT), timeout=5)

        def read_response() -> str:
            data = b""
            while True:
                chunk = s.recv(1)
                if not chunk:
                    break
                data += chunk
                if data.endswith(b'\r\n'):
                    break
            return data.decode('utf-8', errors='ignore').strip()

        def send_command(cmd: str, expected_code: int) -> str:
            s.sendall(f"{cmd}\r\n".encode('utf-8'))
            response = read_response()
            print(f"C: {cmd} | S: {response}")
            if not response.startswith(str(expected_code)):
                raise Exception(f"SMTP Error (expected {expected_code}): {response}")
            return response

        # 1. Initial greeting
        greeting = read_response()
        print(f"S: {greeting}")
        if not greeting.startswith('220'):
            raise Exception("Did not receive 220 initial greeting.")

        # 2. EHLO
        send_command(f"EHLO {APP_DOMAIN}", 250)

        # 3. MAIL FROM
        send_command(f"MAIL FROM:<{mail_from}>", 250)

        # 4. RCPT TO (multiple allowed)
        for rcpt in rcpt_to_list:
            send_command(f"RCPT TO:<{rcpt}>", 250)

        # 5. DATA
        send_command("DATA", 354)

        # Normalize line endings to CRLF
        data_to_send = raw_message.replace('\r\n', '\n').replace('\n', '\r\n')

        # Dot-stuffing: any line that begins with "." gets another "."
        stuffed_lines = []
        for line in data_to_send.split('\r\n'):
            if line.startswith('.'):
                stuffed_lines.append('.' + line)
            else:
                stuffed_lines.append(line)
        stuffed_data = '\r\n'.join(stuffed_lines) + '\r\n'

        # Send DATA block
        s.sendall(stuffed_data.encode('utf-8'))

        # Terminate DATA with "."
        send_command(".", 250)

        # QUIT
        send_command("QUIT", 221)

        return True

    except Exception as e:
        print(f"[SMTP CLIENT] Error during SMTP conversation: {e}")
        return False
    finally:
        if s:
            s.close()


# ========== AUTH ENDPOINTS ==========

@app.route('/api/register', methods=['POST'])
def handle_register():
    data = request.json or {}
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"status": "error", "message": "Email and password are required"}), 400

    if not email.endswith(f"@{APP_DOMAIN}"):
        return jsonify({
            "status": "error",
            "message": f"Registration only allowed for @{APP_DOMAIN} domain"
        }), 403

    if email in users:
        return jsonify({"status": "error", "message": "User already exists"}), 409

    users[email] = {
        "id": str(uuid.uuid4()),
        "password": conceptual_hash(password)
    }

    print(f"[AUTH] Registered user: {email}")
    return jsonify({"status": "success", "message": "Registration successful"}), 201


@app.route('/api/login', methods=['POST'])
def handle_login():
    data = request.json or {}
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"status": "error", "message": "Email and password are required"}), 400

    if conceptual_verify(email, password):
        token = generate_session_token(email)
        print(f"[AUTH] User logged in: {email}")
        return jsonify({"status": "success", "token": token, "email": email}), 200

    return jsonify({"status": "error", "message": "Invalid credentials"}), 401


# ========== MAIL ENDPOINTS ==========

@app.route('/api/send', methods=['POST'])
def handle_send_mail():
    """
    Send a new email. JSON body:
    {
        "token": "<session_token>",
        "rcpt_to": "bob@mydomain.com",
        "subject": "Hello",
        "body": "Test body"
    }
    """
    data = request.json or {}
    token = data.get('token')
    sender = get_user_from_token(token)
    recipient = data.get('rcpt_to')
    subject = data.get('subject')
    body = data.get('body')

    if not sender:
        return jsonify({"status": "error", "message": "Authentication required"}), 401

    if not recipient or not subject or not body:
        return jsonify({"status": "error", "message": "Missing fields"}), 400

    # Build RFC 5322 email using EmailMessage
    msg = EmailMessage()
    msg['From'] = sender
    msg['To'] = recipient
    msg['Subject'] = subject
    msg['Date'] = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid(domain=APP_DOMAIN)
    msg['MIME-Version'] = '1.0'
    msg.set_content(body)  # text/plain; utf-8 by default with modern policy

    raw_message = msg.as_string()

    success = send_smtp_message(sender, [recipient], raw_message)

    if success:
        return jsonify({"status": "success", "message": "Email queued for delivery"}), 200
    else:
        return jsonify({"status": "error", "message": "SMTP server connection failed or protocol error"}), 500


from email import policy
from email.parser import BytesParser

@app.route('/api/inbox/<user_address>', methods=['GET'])
def handle_get_inbox(user_address):
    clean_address = user_address.replace('@', '_').replace('<', '_').replace('>', '_')
    search_pattern = f"*{clean_address}*.eml"
    inbox_files = glob.glob(os.path.join(MAIL_SPOOL_DIR, search_pattern))

    messages = []

    for filename in inbox_files:
        try:
            with open(filename, 'rb') as f:
                raw = f.read()

            # 🔥 Important: strip any leading blank lines
            raw = raw.lstrip(b'\r\n')

            msg = BytesParser(policy=policy.default).parsebytes(raw)

            if msg.is_multipart():
                part = msg.get_body(preferencelist=('plain',))
                body_content = part.get_content() if part else ''
            else:
                body_content = msg.get_content()

            messages.append({
                "id": os.path.basename(filename),
                "from": msg.get('From', 'Unknown Sender'),
                "subject": msg.get('Subject', 'No Subject'),
                "date": msg.get('Date', 'Unknown Date'),
                "body": (body_content or '').strip()
            })
        except Exception as e:
            print(f"[INBOX] Error reading/parsing file {filename}: {e}")

    return jsonify(messages), 200


if __name__ == '__main__':
    # Ensure spool directory exists (C++ server should use the same path)
    if not os.path.exists(MAIL_SPOOL_DIR):
        os.makedirs(MAIL_SPOOL_DIR)

    print(f"API Proxy starting. Target SMTP: {SMTP_SERVER_HOST}:{SMTP_SERVER_PORT}")
    app.run(host='127.0.0.1', port=8000, debug=True)
