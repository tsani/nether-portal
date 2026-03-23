import logging
import os
import subprocess
from datetime import datetime

from flask import Blueprint, request, jsonify
import requests

from .models import Exercise, KG_TO_LBS, Set, SetKind, Workout, make_set_data

OBSIDIAN_WORKOUT_DIR = os.environ['OBSIDIAN_WORKOUT_DIR']
HEVY_WEBHOOK_SECRET = os.environ['HEVY_WEBHOOK_SECRET']
HEVY_API_KEY = os.environ['HEVY_API_KEY']

HEVY_API_BASE = 'https://api.hevyapp.com/v1'

bp = Blueprint('hevy', __name__)

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

@bp.post('/hevy')
def hevy():
    logging.info('hevy webhook: headers=%s', dict(request.headers))

    if request.headers.get('Authorization') != HEVY_WEBHOOK_SECRET:
        return jsonify({'error': 'unauthorized'}), 401

    body = request.get_json(force=True)
    logging.info('hevy webhook: body=%s', body)
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
