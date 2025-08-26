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
from datetime import datetime, timedelta

GITHUB_REPO = os.environ.get('REPO') or os.environ.get('GITHUB_REPOSITORY')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN') or os.environ.get('PERSONAL_GITHUB_TOKEN')

if not GITHUB_REPO or not GITHUB_TOKEN:
    print("ERROR: REPO or GITHUB_TOKEN environment variables not set", file=sys.stderr)
    sys.exit(2)

LORE_INDEX = 'https://lore.kernel.org/linux-cifs/'

DISPATCHED_FILE = '.github/scripts/dispatched.json'

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

def fetch_recent_msgs(hours=2):
    # This section lists the recent messages by scraping the simple index.
    # We fetch the index page and extract message ids that look like <...@...>
    try:
        r = requests.get(LORE_INDEX, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print('ERROR fetching lore index:', e)
        return []

    # find message links like /<MSGID>/
    msg_paths = re.findall(r'href="/linux-cifs/([^/"]+)/"', r.text)
    # message path looks like message id encoded - we'll fetch the message headers to check age
    results = []
    now = datetime.utcnow()
    for path in msg_paths[:50]:
        msg_url = f'https://lore.kernel.org/linux-cifs/{path}/raw'
        try:
            mr = requests.head(msg_url, timeout=10)
            if mr.status_code != 200:
                continue
            # fetch full message and look for headers
            mfull = requests.get(msg_url, timeout=20)
            text = mfull.text
            mid_match = re.search(r'^Message-ID:\s*(.+)$', text, flags=re.M | re.I)
            date_match = re.search(r'^Date:\s*(.+)$', text, flags=re.M | re.I)
            from_match = re.search(r'^From:\s*(.+)$', text, flags=re.M | re.I)
            subj_match = re.search(r'^Subject:\s*(.+)$', text, flags=re.M | re.I)
            in_reply = re.search(r'^In-Reply-To:\s*(.+)$', text, flags=re.M | re.I)
            references = re.search(r'^References:\s*(.+)$', text, flags=re.M | re.I)
            if not mid_match:
                continue
            mid = mid_match.group(1).strip()
            date_str = date_match.group(1).strip() if date_match else ''
            try:
                # best-effort parse
                msg_date = datetime.strptime(date_str[:25], '%a, %d %b %Y %H:%M:%S')
            except Exception:
                msg_date = now

            # consider only recent messages
            if now - msg_date > timedelta(hours=hours):
                continue

            # Determine a thread key using In-Reply-To or the first References id; fallback to own id
            def first_msgid(s: str) -> str:
                m = re.search(r'<[^>]+>', s)
                return m.group(0) if m else ''
            thread_key = ''
            if in_reply:
                thread_key = first_msgid(in_reply.group(1))
            if not thread_key and references:
                thread_key = first_msgid(references.group(1))
            if not thread_key:
                thread_key = mid

            results.append({
                'message_id': mid,
                'thread_key': thread_key,
                'url': msg_url,
                'from': from_match.group(1).strip() if from_match else '',
                'subject': subj_match.group(1).strip() if subj_match else '',
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
    recent = fetch_recent_msgs(hours=6)
    if not recent:
        print('No recent messages found')
        return

    # Group by thread key
    by_thread = {}
    for r in recent:
        by_thread.setdefault(r['thread_key'], []).append(r)

    new = []
    for thread_key, msgs in by_thread.items():
        # Only consider threads where any subject contains "[PATCH"
        if not any('[patch' in (m.get('subject','').lower()) for m in msgs):
            continue

        root_mid = thread_key  # dispatch using the thread root message-id
        if root_mid in dispatched:
            continue

        # Choose author from cover letter if available, else from root id match, else first
        cover = None
        for m in msgs:
            subj = (m.get('subject') or '').lower()
            if '[patch' in subj and ('0/' in subj or 'cover' in subj):
                cover = m
                break
        chosen = cover or next((m for m in msgs if m.get('message_id') == root_mid), msgs[0])
        name, email = parse_from_header(chosen.get('from', ''))
        if not email:
            email = f'bot@{GITHUB_REPO.split("/")[0]}.github'

        ok = trigger_workflow(root_mid, name or 'Patch Author', email)
        if ok:
            dispatched.add(root_mid)
            new.append(root_mid)

    if new:
        save_dispatched(dispatched)
        print('Dispatched patch threads:', new)
    else:
        print('No new patch threads dispatched')

if __name__ == '__main__':
    main()
