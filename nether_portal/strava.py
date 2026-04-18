import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
import sys

import requests
from flask import Blueprint, redirect, request, jsonify
from stravalib import Client

def die(msg):
    logging.error(msg)
    sys.exit(1)

STRAVA_CLIENT_ID = os.environ['STRAVA_CLIENT_ID']
STRAVA_CLIENT_SECRET = os.environ['STRAVA_CLIENT_SECRET']
STRAVA_TOKEN_FILE = os.environ.get('STRAVA_TOKEN_FILE', 'strava_token.json')
STRAVA_VERIFY_TOKEN = os.environ['STRAVA_VERIFY_TOKEN']

OBSIDIAN_VAULT_PATH = os.environ['OBSIDIAN_VAULT_PATH']
OBSIDIAN_ACTIVITY_DIR = os.path.join(
    OBSIDIAN_VAULT_PATH,
    os.environ['OBSIDIAN_ACTIVITY_DIR'],
)
os.path.exists(OBSIDIAN_ACTIVITY_DIR) or \
    die(f'does not exist: {OBSIDIAN_ACTIVITY_DIR}')

STRAVA_API_BASE = 'https://www.strava.com/api/v3'

bp = Blueprint('strava', __name__)

# --- Token management ---

def _save_tokens(token_response):
    with open(STRAVA_TOKEN_FILE, 'w') as f:
        json.dump({
            'access_token': token_response['access_token'],
            'refresh_token': token_response['refresh_token'],
            'expires_at': token_response['expires_at'],
        }, f)

    logging.info('saved strava token')

def _get_access_token() -> str:
    """Return a valid access token, refreshing and persisting if expired."""
    with open(STRAVA_TOKEN_FILE) as f:
        token_data = json.load(f)

    if token_data['expires_at'] <= datetime.now(timezone.utc).timestamp():
        client = Client()
        token_response = client.refresh_access_token(
            client_id=STRAVA_CLIENT_ID,
            client_secret=STRAVA_CLIENT_SECRET,
            refresh_token=token_data['refresh_token'],
        )
        _save_tokens(token_response)
        return token_response['access_token']

    return token_data['access_token']

# --- Activity formatting ---

def _fmt_seconds(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

def _format_activity(a) -> str:
    """Format a stravalib DetailedActivity as an Obsidian markdown note."""
    date_str = a.start_date.strftime('%Y-%m-%d')
    distance_km = a.distance / 1000
    avg_speed_kmh = a.average_speed * 3.6
    max_speed_kmh = a.max_speed * 3.6

    lines = [
        '---',
        f'title: "{a.name}"',
        'tags:',
        '  - "#activity"',
        'topics:',
        '  - "[[Strava]]"',
        f'type: {a.type}',
        f'date: {date_str}',
        f'distance_km: {distance_km:.2f}',
        '---',
        '',
        f'- **Distance:** {distance_km:.2f} km',
        f'- **Moving Time:** {_fmt_seconds(a.moving_time)}',
        f'- **Elapsed Time:** {_fmt_seconds(a.elapsed_time)}',
        f'- **Elevation Gain:** {a.total_elevation_gain:.0f} m',
        f'- **Average Speed:** {avg_speed_kmh:.1f} km/h',
        f'- **Max Speed:** {max_speed_kmh:.1f} km/h',
        f'- **PRs:** {a.pr_count}',
    ]

    if a.description:
        lines += ['', a.description]

    return '\n'.join(lines) + '\n'

def _activity_filename(a) -> str:
    return f'{a.start_date.strftime("%Y-%m-%d")} - {a.name}.md'

def record_activity(a):
    filename = _activity_filename(a)
    path = os.path.join(OBSIDIAN_ACTIVITY_DIR, filename)

    if os.path.exists(path):
        logging.info('strava: refusing to add duplicate activity %s', filename)
        return

    subprocess.run(['git', '-C', OBSIDIAN_ACTIVITY_DIR, 'pull', '--rebase'], check=True)

    with open(path, 'w') as f:
        f.write(_format_activity(a))

    subprocess.run(['git', '-C', OBSIDIAN_ACTIVITY_DIR, 'add', filename], check=True)
    subprocess.run(
        ['git', '-C', OBSIDIAN_ACTIVITY_DIR, 'commit', '-m', f'strava: {a.name}'],
        check=True,
    )
    subprocess.run(['git', '-C', OBSIDIAN_ACTIVITY_DIR, 'push'], check=True)

# --- Webhook subscription setup ---

def _ensure_subscription():
    time.sleep(2)  # let the server finish starting before Strava validates the callback

    resp = requests.get(
        f'{STRAVA_API_BASE}/push_subscriptions',
        params={'client_id': STRAVA_CLIENT_ID, 'client_secret': STRAVA_CLIENT_SECRET},
    )
    resp.raise_for_status()
    existing = resp.json()

    if existing:
        logging.info('strava: webhook subscription already exists (id=%s)', existing[0]['id'])
        return

    resp = requests.post(
        f'{STRAVA_API_BASE}/push_subscriptions',
        data={
            'client_id': STRAVA_CLIENT_ID,
            'client_secret': STRAVA_CLIENT_SECRET,
            'callback_url': f"{os.environ['BASE_URL']}/strava-webhook",
            'verify_token': STRAVA_VERIFY_TOKEN,
        },
    )
    resp.raise_for_status()
    logging.info('strava: webhook subscription created (id=%s)', resp.json()['id'])

def start_subscription_thread():
    t = threading.Thread(target=_ensure_subscription, daemon=True)
    t.start()

# --- Auth routes ---

@bp.get('/strava-auth')
def strava_auth():
    client = Client()
    url = client.authorization_url(
        client_id=STRAVA_CLIENT_ID,
        redirect_uri=f"{os.environ['BASE_URL']}/strava-authorization",
    )
    return redirect(url)

@bp.get('/strava-authorization')
def strava_authorization():
    code = request.args.get('code')
    if not code:
        return jsonify({'error': 'missing code'}), 400

    client = Client()
    token_response = client.exchange_code_for_token(
        client_id=STRAVA_CLIENT_ID,
        client_secret=STRAVA_CLIENT_SECRET,
        code=code,
    )
    _save_tokens(token_response)
    logging.info('strava: authorization complete, tokens saved to %s', STRAVA_TOKEN_FILE)
    return jsonify({'status': 'authorized'}), 200

# --- Webhook routes ---

@bp.get('/strava-webhook')
def strava_webhook_verify():
    if request.args.get('hub.verify_token') != STRAVA_VERIFY_TOKEN:
        return jsonify({'error': 'invalid verify token'}), 403
    return jsonify({'hub.challenge': request.args.get('hub.challenge')}), 200

@bp.post('/strava-webhook')
def strava_webhook():
    body = request.get_json(force=True)

    if body.get('object_type') != 'activity' or body.get('aspect_type') != 'create':
        return jsonify({}), 200

    activity_id = body['object_id']
    client = Client(access_token=_get_access_token())
    activity = client.get_activity(activity_id)
    record_activity(activity)
    return jsonify({}), 200
