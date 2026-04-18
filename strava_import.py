#!/usr/bin/env python3
"""One-off script to bulk-import all Strava activities into an Obsidian vault.

Required env vars (same as nether_portal/strava.py):
  STRAVA_CLIENT_ID
  STRAVA_CLIENT_SECRET
  STRAVA_TOKEN_FILE   (default: strava_token.json)
  OBSIDIAN_VAULT_PATH
  OBSIDIAN_ACTIVITY_DIR  (relative to OBSIDIAN_VAULT_PATH)

Optional:
  STRAVA_DELAY_SEC    seconds to sleep between detailed-activity fetches (default: 0.5)
                      Strava allows 200 req/15 min, so 0.5 s gives comfortable headroom.

Flags:
  -f                  force overwrite of existing notes (default: skip)
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import polyline as _polyline
import urllib.parse
from datetime import datetime, timezone

import requests
from stravalib import Client

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

# --- Config from env ---

STRAVA_CLIENT_ID = os.environ['STRAVA_CLIENT_ID']
STRAVA_CLIENT_SECRET = os.environ['STRAVA_CLIENT_SECRET']
STRAVA_TOKEN_FILE = os.environ.get('STRAVA_TOKEN_FILE', 'strava_token.json')

OBSIDIAN_VAULT_PATH = os.environ['OBSIDIAN_VAULT_PATH']
OBSIDIAN_ACTIVITY_DIR = os.path.join(
    OBSIDIAN_VAULT_PATH,
    os.environ['OBSIDIAN_ACTIVITY_DIR'],
)

DELAY_SEC = float(os.environ.get('STRAVA_DELAY_SEC', '0.5'))
MAPBOX_TOKEN = os.environ.get('MAPBOX_TOKEN')

if not os.path.exists(OBSIDIAN_ACTIVITY_DIR):
    logging.error('does not exist: %s', OBSIDIAN_ACTIVITY_DIR)
    sys.exit(1)

# --- Token management (mirrored from strava.py) ---

def _save_tokens(token_response):
    with open(STRAVA_TOKEN_FILE, 'w') as f:
        json.dump({
            'access_token': token_response['access_token'],
            'refresh_token': token_response['refresh_token'],
            'expires_at': token_response['expires_at'],
        }, f)
    logging.info('saved refreshed strava token')

def _get_access_token() -> str:
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

# --- Formatting (mirrored from strava.py) ---

def _to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()

def _fmt_seconds(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

def _format_activity(a) -> str:
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
        logging.info('wrote route image %s', os.path.basename(png_path))
        return png_path
    except Exception as e:
        logging.warning('failed to render route image: %s', e)
        return None

def _activity_filename(a) -> str:
    return f'{_to_local(a.start_date).strftime("%Y-%m-%d")} - {a.name}.md'

# --- Main import logic ---

def main():
    parser = argparse.ArgumentParser(description='Bulk-import Strava activities into Obsidian.')
    parser.add_argument('-f', '--force', action='store_true', help='overwrite existing notes')
    args = parser.parse_args()

    client = Client(access_token=_get_access_token())

    logging.info('fetching activity list from Strava...')
    summaries = list(client.get_activities())
    logging.info('found %d activities', len(summaries))

    # git pull once before writing anything
    subprocess.run(
        ['git', '-C', OBSIDIAN_VAULT_PATH, 'pull', '--rebase'],
        check=True,
    )

    imported = []
    imported_pngs = []
    skipped = 0

    for i, summary in enumerate(summaries, 1):
        filename = _activity_filename(summary)
        path = os.path.join(OBSIDIAN_ACTIVITY_DIR, filename)

        if os.path.exists(path) and not args.force:
            logging.info('[%d/%d] skip (exists): %s', i, len(summaries), filename)
            skipped += 1
            continue

        # Fetch detailed activity for description, precise fields, and full polyline
        logging.info('[%d/%d] importing: %s', i, len(summaries), filename)
        activity = client.get_activity(summary.id)

        with open(path, 'w') as f:
            f.write(_format_activity(activity))

        imported.append(path)

        png_path = _render_route_image(activity, path)
        if png_path:
            imported_pngs.append(png_path)

        if DELAY_SEC > 0:
            time.sleep(DELAY_SEC)

    logging.info('wrote %d new activities (%d skipped)', len(imported), skipped)

    if not imported:
        logging.info('nothing to commit')
        return

    # Stage all new files in one go
    rel_paths = [os.path.relpath(p, OBSIDIAN_VAULT_PATH) for p in imported]
    rel_png_paths = [os.path.relpath(p, OBSIDIAN_VAULT_PATH) for p in imported_pngs]
    subprocess.run(
        ['git', '-C', OBSIDIAN_VAULT_PATH, 'add'] + rel_paths + rel_png_paths,
        check=True,
    )
    subprocess.run(
        ['git', '-C', OBSIDIAN_VAULT_PATH, 'commit', '-m',
         f'strava: bulk import {len(imported)} activities'],
        check=True,
    )
    subprocess.run(
        ['git', '-C', OBSIDIAN_VAULT_PATH, 'push'],
        check=True,
    )
    logging.info('done')

if __name__ == '__main__':
    main()
