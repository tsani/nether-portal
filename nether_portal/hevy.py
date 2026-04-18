import base64
import logging
import os
import random
import re
import string
import subprocess
from datetime import datetime, timezone
import sys

from flask import Blueprint, request, jsonify
import requests

from .models import (
    DurationOnlySet, Exercise, KG_TO_LBS, RepsAndDurationSet, RepsAndWeightSet,
    RepsOnlySet, Set, SetKind, Workout, make_set_data,
)

def die(msg):
    logging.error(msg)
    sys.exit(1)

OBSIDIAN_VAULT_PATH = os.environ['OBSIDIAN_VAULT_PATH']
OBSIDIAN_WORKOUT_DIR = os.path.join(
    OBSIDIAN_VAULT_PATH,
    os.environ['OBSIDIAN_WORKOUT_DIR'],
)
os.path.exists(OBSIDIAN_WORKOUT_DIR) or \
    die(f'no such file or directory: {OBSIDIAN_WORKOUT_DIR}')

HEVY_WEBHOOK_SECRET = os.environ['HEVY_WEBHOOK_SECRET']
HEVY_API_KEY = os.environ['HEVY_API_KEY']

HEVY_API_BASE = 'https://api.hevyapp.com/v1'

NP_USERNAME = os.environ.get('NP_USERNAME')
NP_PASSWORD = os.environ.get('NP_PASSWORD')

_cache: dict[str, dict] = {}  # npid -> {'id': str, 'workout': dict}

bp = Blueprint('hevy', __name__)

def _to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()

def _fmt_seconds(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

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

def _fetch_workouts_for_date(date_str: str) -> list[dict]:
    target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    workouts = []
    page = 1
    while True:
        resp = requests.get(
            f'{HEVY_API_BASE}/workouts',
            headers={'api-key': HEVY_API_KEY},
            params={'page': page, 'pageSize': 10},
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get('workouts', [])
        if not batch:
            break
        found_older = False
        for w in batch:
            wdate = _to_local(datetime.fromisoformat(w['start_time'])).date()
            if wdate == target_date:
                workouts.append(w)
            elif wdate < target_date:
                found_older = True
        if found_older or page >= data.get('page_count', 1):
            break
        page += 1
    return workouts

def _serialize_set(s: Set) -> dict:
    d = {'kind': str(s.kind)}
    if isinstance(s.data, RepsAndWeightSet):
        d['reps'] = s.data.rep_count
        d['weight_lbs'] = s.data.weight_lbs
    elif isinstance(s.data, RepsOnlySet):
        d['reps'] = s.data.rep_count
    elif isinstance(s.data, RepsAndDurationSet):
        d['reps'] = s.data.rep_count
        d['duration_sec'] = s.data.duration_sec
    elif isinstance(s.data, DurationOnlySet):
        d['duration_sec'] = s.data.duration_sec
    return d

def _workout_to_dict(npid: str, hw: dict, workout: Workout) -> dict:
    return {
        'npid': npid,
        'id': hw['id'],
        'timestamp': workout.start_time.isoformat(),
        'elapsed_time': _fmt_seconds(workout.duration),
        'elapsed_time_sec': workout.duration,
        'title': workout.title,
        'exercises': [
            {'name': name, 'sets': [_serialize_set(s) for s in ex.sets]}
            for name, ex in workout.exercises.items()
        ],
    }

def parse_hevy_workout(hw) -> Workout:
    start_time = datetime.fromisoformat(hw['start_time'])
    end_time = datetime.fromisoformat(hw['end_time'])
    duration = int((end_time - start_time).total_seconds())

    exercises = {}
    for ex in hw['exercises']:
        name = ex['title']
        sets = []
        for s in ex['sets']:
            weight_kg = s['weight_kg']
            weight_lbs = round(weight_kg * KG_TO_LBS) if weight_kg else None
            data = make_set_data(
                rep_count=s['reps'],
                weight_lbs=weight_lbs,
                duration_sec=s['duration_seconds'],
            )
            sets.append(Set(data=data, kind=SetKind(s['type'])))
        exercises[name] = Exercise(name=name, sets=sets)

    return Workout(
        title=hw['title'],
        start_time=start_time,
        duration=duration,
        exercises=exercises,
        description=hw['description'],
    )

def record_workout(workout):
    if os.path.exists(os.path.join(OBSIDIAN_WORKOUT_DIR, workout.filename)):
        logging.info("Refusing to add duplicate workout.")
        return # skip duplicate workouts!

    subprocess.run(['git', '-C', OBSIDIAN_WORKOUT_DIR, 'pull', '--rebase'], check=True)

    path = os.path.join(OBSIDIAN_WORKOUT_DIR, workout.filename)
    with open(path, 'w') as f:
        f.write(workout.note_format)

    subprocess.run(['git', '-C', OBSIDIAN_WORKOUT_DIR, 'add', workout.filename], check=True)
    subprocess.run(
        ['git', '-C', OBSIDIAN_WORKOUT_DIR, 'commit', '-m', f'hevy: {workout.title}'],
        check=True,
    )
    subprocess.run(['git', '-C', OBSIDIAN_WORKOUT_DIR, 'push'], check=True)

@bp.get('/hevy/activities')
def hevy_list_activities():
    if not _check_auth():
        return jsonify({'error': 'unauthorized'}), 401

    date_str = request.args.get('date')
    if not date_str:
        return jsonify({'error': 'missing date parameter'}), 400
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'invalid date format, use YYYY-MM-DD'}), 400

    raw_workouts = _fetch_workouts_for_date(date_str)

    global _cache
    _cache = {}
    entries = []
    for hw in raw_workouts:
        npid = _make_npid()
        while npid in _cache:
            npid = _make_npid()
        workout = parse_hevy_workout(hw)
        _cache[npid] = {'id': hw['id'], 'workout': hw}
        entries.append((npid, hw, workout))

    if _wants_markdown():
        lines = [
            f'- **{w.title}** ({npid}) at {_to_local(w.start_time).strftime("%H:%M")} — volume: {w.volume:,} lbs'
            for npid, _, w in entries
        ]
        body = '\n'.join(lines) + '\n' if lines else '(no workouts)\n'
        return body, 200, {'Content-Type': 'text/markdown'}

    return jsonify([_workout_to_dict(npid, hw, w) for npid, hw, w in entries]), 200

@bp.post('/hevy/import')
def hevy_import_activity():
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
        workout_id = cached['id']
    else:
        workout_id = str(activity_id)

    resp = requests.get(
        f'{HEVY_API_BASE}/workouts/{workout_id}',
        headers={'api-key': HEVY_API_KEY},
    )
    if resp.status_code == 404:
        return jsonify({'error': 'workout not found'}), 404
    resp.raise_for_status()

    workout = parse_hevy_workout(resp.json())
    record_workout(workout)
    return jsonify({'status': 'imported', 'title': workout.title}), 200

@bp.post('/hevy')
def hevy():
    if request.headers.get('Authorization') != HEVY_WEBHOOK_SECRET:
        return jsonify({'error': 'unauthorized'}), 401

    body = request.get_json(force=True)
    workout_id = body['workoutId']

    resp = requests.get(
        f'{HEVY_API_BASE}/workouts/{workout_id}',
        headers={'api-key': HEVY_API_KEY},
    )
    if resp.status_code == 404:
        logging.info('hevy webhook: workout %s not found (non-creation event?), ignoring', workout_id)
        return jsonify({}), 200
    resp.raise_for_status()

    workout = parse_hevy_workout(resp.json())
    record_workout(workout)

    return jsonify({}), 200
