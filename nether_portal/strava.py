import base64
import json
import logging
import os
import random
import re
import string
import subprocess
import threading
import time
import polyline as _polyline
import urllib.parse
from datetime import datetime, timedelta, timezone
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

NP_USERNAME = os.environ.get('NP_USERNAME')
NP_PASSWORD = os.environ.get('NP_PASSWORD')
MAPBOX_TOKEN = os.environ.get('MAPBOX_TOKEN')

_cache: dict[str, dict] = {}  # npid -> {'id': int}

bp = Blueprint('strava', __name__)

def _check_auth() -> bool:
    if NP_USERNAME is None or NP_PASSWORD is None:
        return False
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Basic '):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode('utf-8')
        user, pw = decoded.split(':', 1)
        return user == NP_USERNAME and pw == NP_PASSWORD
    except Exception:
        return False

def _wants_markdown() -> bool:
    accept = request.headers.get('Accept', '')
    return 'text/markdown' in accept or 'text/plain' in accept

def _make_npid() -> str:
    return ''.join(random.choices(string.ascii_lowercase, k=4))

def _fetch_activities_for_date(date_str: str):
    target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    after = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    before = after + timedelta(days=1)
    client = Client(access_token=_get_access_token())
    return list(client.get_activities(after=after, before=before))

def _activity_to_dict(npid: str, a) -> dict:
    return {
        'npid': npid,
        'id': a.id,
        'timestamp': a.start_date.isoformat(),
        'elapsed_time': _fmt_seconds(int(a.elapsed_time)) if a.elapsed_time is not None else None,
        'elapsed_time_sec': int(a.elapsed_time) if a.elapsed_time is not None else None,
        'title': a.name,
        'type': str(a.sport_type.root),
        'distance_km': round(float(a.distance) / 1000, 2) if a.distance else None,
        'moving_time': _fmt_seconds(int(a.moving_time)) if a.moving_time is not None else None,
        'moving_time_sec': int(a.moving_time) if a.moving_time is not None else None,
        'elevation_gain_m': round(float(a.total_elevation_gain), 0) if a.total_elevation_gain else None,
        'average_speed_kmh': round(float(a.average_speed) * 3.6, 1) if a.average_speed else None,
        'max_speed_kmh': round(float(a.max_speed) * 3.6, 1) if a.max_speed else None,
        'pr_count': a.pr_count,
    }

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

def _to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()

def _fmt_seconds(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

def _format_activity(a) -> str:
    """Format a stravalib DetailedActivity as an Obsidian markdown note."""
    date_str = _to_local(a.start_date).strftime('%Y-%m-%d')
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
        f'type: "[[{a.sport_type.root}]]"',
        f'date: {date_str}',
        f'distance_km: {distance_km:.2f}',
        f'moving_time_sec: {a.moving_time}',
        f'elapsed_time_sec: {a.elapsed_time}',
        f'moving_time: {_fmt_seconds(a.moving_time)}',
        f'elapsed_time: {_fmt_seconds(a.elapsed_time)}',
        f'elevation_gain_m: {a.total_elevation_gain:.0f}',
        f'average_speed_kmh: {avg_speed_kmh:.1f}',
        f'max_speed_kmh: {max_speed_kmh:.1f}',
        f'personal_record_count: {a.pr_count}',
    ]

    lines.append(f'daily_note: "[[{date_str}]]"')

    png_name = _activity_filename(a).replace('.md', '.png')

    if a.map:
        poly = a.map.polyline or a.map.summary_polyline
        if poly:
            lines.append(f'cover: "[[{png_name}]]"')
            lines.append(f"polyline: '{poly}'")

    lines.append('---')

    body_lines = [f'![[{png_name}]]']

    if a.description:
        body_lines += ['', a.description]

    return '\n'.join(lines) + '\n' + '\n'.join(body_lines) + '\n'

def _activity_filename(a) -> str:
    return f'{_to_local(a.start_date).strftime("%Y-%m-%d")} - {a.name}.md'

def _render_route_image(a, note_path: str) -> str | None:
    """Render the activity polyline as a static map PNG via Mapbox.

    Returns the PNG path if written, None if skipped (no polyline or no token).
    """
    if not MAPBOX_TOKEN:
        return None
    poly = a.map.polyline or a.map.summary_polyline if a.map else None
    if not poly:
        return None

    png_path = note_path[:-3] + '.png'

    # Mapbox has an 8192-char URL limit; downsample until it fits.
    coords = _polyline.decode(poly, precision=5)
    stride = 1
    while True:
        encoded = _polyline.encode(coords[::stride], precision=5)
        url_poly = urllib.parse.quote(encoded, safe='')
        overlay = f'path-4+fc4c02-1({url_poly})'
        url = (
            f'https://api.mapbox.com/styles/v1/mapbox/outdoors-v12/static/'
            f'{overlay}/auto/1280x800@2x'
            f'?padding=50&access_token={MAPBOX_TOKEN}'
        )
        if len(url) <= 8192 or stride > 16:
            break
        stride *= 2

    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        with open(png_path, 'wb') as f:
            f.write(r.content)
        logging.info('strava: wrote route image %s', os.path.basename(png_path))
        return png_path
    except Exception as e:
        logging.warning('strava: failed to render route image: %s', e)
        return None


def record_activity(a):
    filename = _activity_filename(a)
    path = os.path.join(OBSIDIAN_ACTIVITY_DIR, filename)

    if os.path.exists(path):
        logging.info('strava: refusing to add duplicate activity %s', filename)
        return

    subprocess.run(['git', '-C', OBSIDIAN_ACTIVITY_DIR, 'pull', '--rebase'], check=True)

    with open(path, 'w') as f:
        f.write(_format_activity(a))

    png_path = _render_route_image(a, path)

    files_to_add = [filename]
    if png_path:
        files_to_add.append(os.path.basename(png_path))

    subprocess.run(['git', '-C', OBSIDIAN_ACTIVITY_DIR, 'add'] + files_to_add, check=True)
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
    callback_url = f"{os.environ['BASE_URL']}/strava-webhook"
    if any(r['callback_url'] == callback_url for r in existing):
        logging.info('strava: webhook subscription already exists (id=%s)', existing[0]['id'])
        return

    # Delete stale subscriptions whose callback_url doesn't match.
    for sub in existing:
        logging.info('strava: deleting stale subscription id=%s (callback_url=%s)', sub['id'], sub['callback_url'])
        del_resp = requests.delete(
            f"{STRAVA_API_BASE}/push_subscriptions/{sub['id']}",
            params={'client_id': STRAVA_CLIENT_ID, 'client_secret': STRAVA_CLIENT_SECRET},
        )
        del_resp.raise_for_status()

    resp = requests.post(
        f'{STRAVA_API_BASE}/push_subscriptions',
        data={
            'client_id': STRAVA_CLIENT_ID,
            'client_secret': STRAVA_CLIENT_SECRET,
            'callback_url': callback_url,
            'verify_token': STRAVA_VERIFY_TOKEN,
        },
    )
    resp.raise_for_status()
    logging.info('strava: webhook subscription created (id=%s)', resp.json()['id'])

def start_subscription_thread():
    t = threading.Thread(target=_ensure_subscription, daemon=True)
    t.start()

# --- Manual import routes ---

@bp.get('/strava/activities')
def strava_list_activities():
    if not _check_auth():
        return jsonify({'error': 'unauthorized'}), 401

    date_str = request.args.get('date')
    if not date_str:
        return jsonify({'error': 'missing date parameter'}), 400
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'invalid date format, use YYYY-MM-DD'}), 400

    activities = _fetch_activities_for_date(date_str)

    global _cache
    _cache = {}
    entries = []
    for a in activities:
        npid = _make_npid()
        while npid in _cache:
            npid = _make_npid()
        _cache[npid] = {'id': a.id}
        entries.append((npid, a))

    if _wants_markdown():
        lines = []
        for npid, a in entries:
            time_str = _to_local(a.start_date).strftime('%H:%M')
            dist = f'{float(a.distance) / 1000:.1f} km' if a.distance else 'unknown distance'
            lines.append(f'- **{a.name}** ({npid}) at {time_str} — {dist}')
        body = '\n'.join(lines) + '\n' if lines else '(no activities)\n'
        return body, 200, {'Content-Type': 'text/markdown'}

    return jsonify([_activity_to_dict(npid, a) for npid, a in entries]), 200

@bp.post('/strava/import')
def strava_import_activity():
    if not _check_auth():
        return jsonify({'error': 'unauthorized'}), 401

    body = request.get_json(force=True)
    activity_id = body.get('id')
    if not activity_id:
        return jsonify({'error': 'missing id'}), 400

    if re.match(r'^[a-z]{4}$', str(activity_id)):
        cached = _cache.get(str(activity_id))
        if not cached:
            return jsonify({'error': 'npid not found in cache'}), 404
        native_id = cached['id']
    else:
        try:
            native_id = int(activity_id)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid activity id'}), 400

    client = Client(access_token=_get_access_token())
    activity = client.get_activity(native_id)
    record_activity(activity)
    return jsonify({'status': 'imported', 'title': activity.name}), 200

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
