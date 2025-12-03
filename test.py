# test_client.py
# Requires: pip install requests

import time
import requests

BASE_URL = "http://127.0.0.1:8000"


def register(email, password):
    print(f"\n[TEST] Registering {email}")
    r = requests.post(f"{BASE_URL}/api/register", json={
        "email": email,
        "password": password
    })
    print(f"Status: {r.status_code}, Response: {r.json()}")


def login(email, password):
    print(f"\n[TEST] Logging in {email}")
    r = requests.post(f"{BASE_URL}/api/login", json={
        "email": email,
        "password": password
    })
    print(f"Status: {r.status_code}, Response: {r.json()}")
    if r.status_code == 200:
        return r.json().get("token")
    return None


def send_mail(token, rcpt_to, subject, body):
    print(f"\n[TEST] Sending mail to {rcpt_to}")
    r = requests.post(f"{BASE_URL}/api/send", json={
        "token": token,
        "rcpt_to": rcpt_to,
        "subject": subject,
        "body": body
    })
    print(f"Status: {r.status_code}, Response: {r.json()}")


def get_inbox(user_address):
    print(f"\n[TEST] Fetching inbox for {user_address}")
    r = requests.get(f"{BASE_URL}/api/inbox/{user_address}")
    print(f"Status: {r.status_code}")
    try:
        messages = r.json()
    except Exception:
        print("Failed to decode JSON:", r.text)
        return

    if not messages:
        print("No messages found.")
        return

    for i, msg in enumerate(messages, start=1):
        print("\n----- MESSAGE", i, "-----")
        print("ID     :", msg.get("id"))
        print("From   :", msg.get("from"))
        print("Subject:", msg.get("subject"))
        print("Date   :", msg.get("date"))
        print("Body   :")
        print(msg.get("body"))
        print("-------------------------")


if __name__ == "__main__":
    # Make sure your C++ SMTP server is running first
    # Then start server.py, and finally run this test script.

    alice_email = "alice@mydomain.com"
    bob_email = "bob@mydomain.com"
    password = "password123"

    # 1. Register both users (second run will probably show "User already exists")
    register(alice_email, password)
    register(bob_email, password)

    # 2. Login as Alice
    alice_token = login(alice_email, password)
    if not alice_token:
        print("Could not log in as Alice; aborting tests.")
        exit(1)

    # 3. Send an email from Alice -> Bob
    send_mail(
        token=alice_token,
        rcpt_to=bob_email,
        subject="Test Email from Alice",
        body="Hello Bob,\n\nThis is a test message sent via C++ SMTP server.\n\nRegards,\nAlice"
    )

    # 4. Give the C++ server a moment to write the .eml file (if needed)
    time.sleep(1)

    # 5. Fetch Bob's inbox
    get_inbox(bob_email)
