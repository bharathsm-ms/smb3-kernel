#!/usr/bin/env python3
"""Fetch recent patches from lore.kernel.org and dispatch the codereview workflow.

This script checks the linux-cifs mailing list index on lore.kernel.org for recent
messages with patches, and triggers the `codereview.yml` workflow_dispatch with the
required inputs for patches that haven't been dispatched before.

Assumptions:
- A repository secret `PERSONAL_GITHUB_TOKEN` exists and has workflow dispatch permissions.
- This script runs inside the repository root.
"""

import os
import sys
import re
import json
import requests
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime, getaddresses
from email import policy
from email.parser import Parser

GITHUB_REPO = os.environ.get('REPO') or os.environ.get('GITHUB_REPOSITORY')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN') or os.environ.get('PERSONAL_GITHUB_TOKEN')

if not GITHUB_REPO or not GITHUB_TOKEN:
    print("ERROR: REPO or GITHUB_TOKEN environment variables not set", file=sys.stderr)
    sys.exit(2)

LORE_INDEX = 'https://lore.kernel.org/linux-cifs/'
LIST_ADDR = 'linux-cifs@vger.kernel.org'
LIST_ID = 'linux-cifs.vger.kernel.org'
HEADERS = {
    'User-Agent': 'smb3-kernel-bot/1.0 (+https://github.com/bharathsm-ms/smb3-kernel)',
    'Accept': '*/*',
}

DISPATCHED_FILE = '.github/scripts/dispatched.json'
SCAN_HOURS = int(os.environ.get('SCAN_HOURS', '24'))
MAX_MSGS = int(os.environ.get('MAX_MSGS', '100'))

def load_dispatched():
    try:
        with open(DISPATCHED_FILE, 'r') as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_dispatched(s):
    try:
        os.makedirs(os.path.dirname(DISPATCHED_FILE), exist_ok=True)
        with open(DISPATCHED_FILE, 'w') as f:
            json.dump(sorted(list(s)), f)
    except Exception as e:
        print("WARN: failed to save dispatched list:", e)

def fetch_recent_msgs(hours=2, max_msgs=50):
    # This section lists the recent messages by scraping the simple index.
    # We fetch the index page and extract message ids that look like <...@...>
    try:
        r = requests.get(LORE_INDEX, timeout=30, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print('ERROR fetching lore index:', e)
        return []

    # find message links like /<MSGID>/
    msg_paths = re.findall(r'href="/linux-cifs/([^/"]+)/"', r.text)
    # message path looks like message id encoded - we'll fetch the message headers to check age
    results = []
    now = datetime.now(timezone.utc)
    for path in msg_paths[:max_msgs]:
        msg_url = f'https://lore.kernel.org/linux-cifs/{path}/raw'
        try:
            # fetch full message; HEAD is often blocked
            mfull = requests.get(msg_url, timeout=20, headers=HEADERS)
            if mfull.status_code != 200:
                continue
            text = mfull.text
            msg = Parser(policy=policy.default).parsestr(text)

            mid = (msg.get('message-id') or '').strip()
            if not mid:
                continue
            date_str = (msg.get('date') or '').strip()
            try:
                dt = parsedate_to_datetime(date_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                msg_date = dt.astimezone(timezone.utc)
            except Exception:
                msg_date = now

            # consider only recent messages
            if now - msg_date > timedelta(hours=hours):
                continue

            # Only accept messages that were actually sent to the list (not just CC):
            # - To: contains linux-cifs@vger.kernel.org
            # - List-Id: contains linux-cifs.vger.kernel.org
            to_addrs = [addr.lower() for _, addr in getaddresses([msg.get('to', '')])]
            to_ok = LIST_ADDR in to_addrs
            listid_ok = LIST_ID in (msg.get('list-id', '') or '').lower()
            if not (to_ok and listid_ok):
                continue

            # Determine a thread key using In-Reply-To or the first References id; fallback to own id
            def first_msgid(s: str) -> str:
                m = re.search(r'<[^>]+>', s or '')
                return m.group(0) if m else ''
            thread_key = ''
            in_reply = msg.get('in-reply-to')
            if in_reply:
                thread_key = first_msgid(in_reply)
            if not thread_key:
                # References may contain multiple; take the first
                refs = msg.get('references')
                thread_key = first_msgid(refs)
            if not thread_key:
                thread_key = mid

            results.append({
                'message_id': mid,
                'thread_key': thread_key,
                'url': msg_url,
                'from': (msg.get('from') or '').strip(),
                'subject': (msg.get('subject') or '').strip(),
                'date': msg_date.timestamp(),
            })
        except Exception:
            continue

    return results

def trigger_workflow(message_id, author_name, author_email):
    api = f'https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/codereview.yml/dispatches'
    payload = {
        'ref': 'for-next',
        'inputs': {
            'message_id': message_id,
            'author_name': author_name,
            'author_email': author_email,
        }
    }
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github+json'
    }
    r = requests.post(api, json=payload, headers=headers, timeout=20)
    if r.status_code in (204, 201):
        print('Dispatched', message_id)
        return True
    else:
        print('Failed to dispatch', message_id, r.status_code, r.text)
        return False

def parse_from_header(header):
    # Try to split 'Name <email@host>'
    m = re.match(r'(?P<name>.+)\s+<(?P<email>[^>]+)>', header)
    if m:
        return m.group('name').strip(), m.group('email').strip()
    return header, ''

def main():
    dispatched = load_dispatched()
    hours = SCAN_HOURS
    max_msgs = MAX_MSGS
    recent = fetch_recent_msgs(hours=hours, max_msgs=max_msgs)
    if not recent:
        print('No recent messages found')
        return

    # Helper to detect single-patch subjects
    def is_single_patch(subj: str) -> bool:
        s = (subj or '').lower()
        if '[patch' not in s:
            return False
        # exclude cover letters
        if '0/' in s:
            return False
        m = re.search(r'(\d+)/(\d+)', s)
        if m:
            return m.group(1) == '1' and m.group(2) == '1'
        return True

    # Keep only single-patch messages
    single = [m for m in recent if is_single_patch(m.get('subject',''))]
    if not single:
        print('No recent single-patch messages found')
        return

    # Pick the most recent by date
    chosen = max(single, key=lambda m: m.get('date', 0))
    mid = chosen['message_id']
    if mid in dispatched:
        print('Latest single patch already dispatched:', mid)
        return

    name, email = parse_from_header(chosen.get('from', ''))
    if not email:
        email = f'bot@{GITHUB_REPO.split("/")[0]}.github'

    if trigger_workflow(mid, name or 'Patch Author', email):
        dispatched.add(mid)
        save_dispatched(dispatched)
        print('Dispatched latest single patch:', mid)
    else:
        print('Failed to dispatch latest single patch')

if __name__ == '__main__':
    main()
