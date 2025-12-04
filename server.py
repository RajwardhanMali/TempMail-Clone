# server.py
# Requires: pip install flask flask-cors
#
# This is a disposable email service API that talks to a toy SMTP server
# implemented in C++ on 127.0.0.1:2525. It:
#   - Creates random disposable email addresses
#   - Sends mail via SMTP to the C++ server
#   - Reads .eml files from a spool directory and exposes them as JSON

import os
import glob
import socket
import uuid

from flask import Flask, request, jsonify
from flask_cors import CORS

from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from email import policy
from email.parser import BytesParser

# --- Configuration (must match C++ SMTP server & spool path) ---
SMTP_SERVER_HOST = "127.0.0.1"
SMTP_SERVER_PORT = 2525
MAIL_SPOOL_DIR = "mail_spool"
APP_DOMAIN = "mydomain.com"

app = Flask(__name__)
CORS(app)


# =====================================================================
#                    SMTP CLIENT: PYTHON → C++ SERVER
# =====================================================================

def send_smtp_message(mail_from: str, rcpt_to_list, raw_message: str) -> bool:
    """
    Connect to the C++ SMTP server and send a complete RFC 5322 message.
    Executes:
        220 Greeting
        EHLO
        MAIL FROM
        RCPT TO (for each recipient)
        DATA + message
        .
        QUIT
    """
    s = None
    try:
        s = socket.create_connection(
            (SMTP_SERVER_HOST, SMTP_SERVER_PORT), timeout=5
        )

        def read_response() -> str:
            data = b""
            while True:
                chunk = s.recv(1)
                if not chunk:
                    break
                data += chunk
                if data.endswith(b"\r\n"):
                    break
            return data.decode("utf-8", errors="ignore").strip()

        def send_command(cmd: str, expected_code: int) -> str:
            s.sendall(f"{cmd}\r\n".encode("utf-8"))
            response = read_response()
            print(f"C: {cmd} | S: {response}")
            if not response.startswith(str(expected_code)):
                raise Exception(f"SMTP Error (expected {expected_code}): {response}")
            return response

        # 1. Greeting
        greeting = read_response()
        print(f"S: {greeting}")
        if not greeting.startswith("220"):
            raise Exception("Did not receive 220 initial greeting")

        # 2. EHLO
        send_command(f"EHLO {APP_DOMAIN}", 250)

        # 3. MAIL FROM
        send_command(f"MAIL FROM:<{mail_from}>", 250)

        # 4. RCPT TO
        for rcpt in rcpt_to_list:
            send_command(f"RCPT TO:<{rcpt}>", 250)

        # 5. DATA
        send_command("DATA", 354)

        # Normalize line endings to CRLF
        data_to_send = raw_message.replace("\r\n", "\n").replace("\n", "\r\n")

        # Dot-stuffing (RFC 5321): lines beginning with "." are prefixed with another "."
        stuffed_lines = []
        for line in data_to_send.split("\r\n"):
            if line.startswith("."):
                stuffed_lines.append("." + line)
            else:
                stuffed_lines.append(line)
        stuffed_data = "\r\n".join(stuffed_lines) + "\r\n"

        # Send DATA block
        s.sendall(stuffed_data.encode("utf-8"))

        # End of DATA
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


# =====================================================================
#                   DISPOSABLE EMAIL API ENDPOINTS
# =====================================================================

@app.route("/api/new_mailbox", methods=["POST"])
def handle_new_mailbox():
    """
    Create a new disposable email address.
    No auth, no password. Just a random alias under APP_DOMAIN.
    Returns:
        { "email": "abcd1234@mydomain.com" }
    """
    alias = uuid.uuid4().hex[:8]  # 8-char random mailbox
    email = f"{alias}@{APP_DOMAIN}"
    return jsonify({"email": email}), 200


@app.route("/api/send", methods=["POST"])
def handle_send_mail():
    """
    Send an email through the C++ SMTP server.

    Expected JSON:
    {
        "from": "sender@example.com",
        "rcpt_to": "xyz123@mydomain.com",
        "subject": "Hello",
        "body": "Message body..."
    }

    No authentication (disposable service).
    """
    data = request.json or {}
    sender = data.get("from")
    recipient = data.get("rcpt_to")
    subject = data.get("subject")
    body = data.get("body")

    if not sender or not recipient or not subject or not body:
        return (
            jsonify(
                {"status": "error", "message": "from, rcpt_to, subject, body are required"}
            ),
            400,
        )

    # OPTIONAL restriction: only allow local disposable recipients
    # if not recipient.endswith(f"@{APP_DOMAIN}"):
    #     return jsonify({"status": "error",
    #                     "message": f"Recipient must be under @{APP_DOMAIN}"}), 400

    # Build a proper RFC 5322 email
    msg = EmailMessage()
    msg["From"] = sender  # only address, no display name
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=APP_DOMAIN)
    msg["MIME-Version"] = "1.0"
    msg.set_content(body)  # text/plain; charset="utf-8"

    raw_message = msg.as_string()

    success = send_smtp_message(sender, [recipient], raw_message)

    if success:
        return (
            jsonify({"status": "success", "message": "Email queued for delivery"}),
            200,
        )
    else:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "SMTP server connection failed or protocol error",
                }
            ),
            500,
        )


@app.route("/api/inbox/<user_address>", methods=["GET"])
def handle_get_inbox(user_address):
    """
    Return all messages for a given email address as JSON.

    URL example:
        GET /api/inbox/abcd1234@mydomain.com
    """
    # sanitize for filename pattern
    clean_address = (
        user_address.replace("@", "_").replace("<", "_").replace(">", "_")
    )

    search_pattern = f"*{clean_address}*.eml"
    inbox_files = glob.glob(os.path.join(MAIL_SPOOL_DIR, search_pattern))

    messages = []

    for filename in inbox_files:
        try:
            with open(filename, "rb") as f:
                raw = f.read()

            # be tolerant to any accidental leading blank lines
            raw = raw.lstrip(b"\r\n")

            msg = BytesParser(policy=policy.default).parsebytes(raw)

            # Extract text/plain body
            if msg.is_multipart():
                part = msg.get_body(preferencelist=("plain",))
                body_content = part.get_content() if part else ""
            else:
                body_content = msg.get_content()

            messages.append(
                {
                    "id": os.path.basename(filename),
                    "from": msg.get("From", "Unknown Sender"),
                    "subject": msg.get("Subject", "No Subject"),
                    "date": msg.get("Date", "Unknown Date"),
                    "body": (body_content or "").strip(),
                }
            )
        except Exception as e:
            print(f"[INBOX] Error reading/parsing file {filename}: {e}")

    return jsonify(messages), 200


# =====================================================================
#                              MAIN
# =====================================================================

if __name__ == "__main__":
    # Make sure the spool directory exists
    if not os.path.exists(MAIL_SPOOL_DIR):
        os.makedirs(MAIL_SPOOL_DIR)

    print(
        f"Disposable Email API starting. "
        f"Target SMTP: {SMTP_SERVER_HOST}:{SMTP_SERVER_PORT}"
    )
    app.run(host="127.0.0.1", port=8000, debug=True)
