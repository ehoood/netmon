#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_send.py - Minimal Telegram Bot API sender (stdlib only).

Reusable for any bot. Sends a text message and/or a document (e.g. the HTML
report). Credentials come from (in order): CLI flags, netmon.conf, env vars
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.

Config file format (KEY=VALUE, '#' comments allowed):
    BOT_TOKEN=123456:AA...your_token...
    CHAT_ID=123456789

Usage:
    ./telegram_send.py --text "hello"
    ./telegram_send.py --document report.html --caption "weekly report"
    ./telegram_send.py --text "hi" --dry-run          # print, don't send
"""

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

API = "https://api.telegram.org/bot{token}/{method}"


def load_config(path):
    cfg = {}
    if path and os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def resolve_creds(args):
    cfg = load_config(args.config)
    token = args.token or cfg.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = args.chat_id or cfg.get("CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    return token, chat


def _post(url, data, headers, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_message(token, chat_id, text, parse_mode="HTML", timeout=30):
    url = API.format(token=token, method="sendMessage")
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    return _post(url, body, {"Content-Type": "application/x-www-form-urlencoded"}, timeout)


def send_document(token, chat_id, path, caption="", timeout=120):
    url = API.format(token=token, method="sendDocument")
    boundary = "----netmon" + uuid.uuid4().hex
    filename = os.path.basename(path)
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        file_bytes = f.read()

    parts = []
    def field(name, value):
        parts.append(("--" + boundary).encode())
        parts.append(('Content-Disposition: form-data; name="%s"' % name).encode())
        parts.append(b"")
        parts.append(str(value).encode("utf-8"))

    field("chat_id", chat_id)
    if caption:
        field("caption", caption[:1024])
        field("parse_mode", "HTML")

    parts.append(("--" + boundary).encode())
    parts.append(('Content-Disposition: form-data; name="document"; filename="%s"' % filename).encode())
    parts.append(("Content-Type: %s" % ctype).encode())
    parts.append(b"")
    parts.append(file_bytes)
    parts.append(("--" + boundary + "--").encode())
    parts.append(b"")

    body = b"\r\n".join(parts)
    headers = {"Content-Type": "multipart/form-data; boundary=%s" % boundary}
    return _post(url, body, headers, timeout)


def main():
    ap = argparse.ArgumentParser(description="Send a Telegram message and/or document")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "netmon.conf"))
    ap.add_argument("--token")
    ap.add_argument("--chat-id")
    ap.add_argument("--text")
    ap.add_argument("--document")
    ap.add_argument("--caption", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token, chat = resolve_creds(args)
    if args.dry_run:
        print("[dry-run] token=%s chat=%s" % ("SET" if token else "MISSING", chat))
        if args.text:
            print("[dry-run] would sendMessage:\n%s" % args.text)
        if args.document:
            print("[dry-run] would sendDocument: %s (caption=%r)" % (args.document, args.caption))
        return 0

    if not token or not chat:
        print("ERROR: missing BOT_TOKEN/CHAT_ID (use --config, flags, or env vars)", file=sys.stderr)
        return 2

    try:
        if args.text:
            r = send_message(token, chat, args.text)
            print("sendMessage ok=%s" % r.get("ok"))
        if args.document:
            r = send_document(token, chat, args.document, args.caption)
            print("sendDocument ok=%s" % r.get("ok"))
    except urllib.error.HTTPError as e:
        print("Telegram HTTP error %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:300]), file=sys.stderr)
        return 1
    except Exception as e:
        print("Telegram send failed: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
