import logging
import os
import subprocess
from flask import Flask, request, jsonify
import requests
from dataclasses import dataclass
from enum import StrEnum, auto
from functools import cached_property
from datetime import datetime

app = Flask(__name__)

OBSIDIAN_WORKOUT_DIR = os.environ['OBSIDIAN_WORKOUT_DIR']
HEVY_WEBHOOK_SECRET = os.environ['HEVY_WEBHOOK_SECRET']
HEVY_API_KEY = os.environ['HEVY_API_KEY']

HEVY_API_BASE = 'https://api.hevyapp.com/v1'

KG_TO_LBS = 2.20462

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

@app.post('/hevy')
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

@dataclass
class RepsAndWeightSet:
    rep_count: int
    weight_lbs: int

    def __str__(self): return f'{self.rep_count} x {self.weight_lbs} lbs'

    @property
    def volume(self): return self.rep_count * self.weight_lbs

@dataclass
class RepsOnlySet:
    rep_count: int

    def __str__(self): return f'{self.rep_count}'

    @property
    def volume(self): return 0

@dataclass
class RepsAndDurationSet:
    rep_count: int
    duration_sec: int

    def __str__(self): return f'{self.rep_count} x {self.duration_sec} sec'

    @property
    def volume(self): return 0

@dataclass
class DurationOnlySet:
    duration_sec: int

    def __str__(self): return f'{self.duration_sec} sec'

    @property
    def volume(self): return 0

SetData = DurationOnlySet | RepsAndDurationSet | RepsOnlySet | RepsAndDurationSet

class SetKind(StrEnum):
    warmup = auto()
    normal = auto()
    failure = auto()
    dropset = auto()

@dataclass
class Set:
    data: SetData
    kind: SetKind

    @property
    def volume(self):
        return self.data.volume

    @property
    def note_format(self):
        return f'{str(self.data)}' \
            + ('' if self.kind == 'normal' else f' ({self.kind})')

def make_set_data(rep_count=None, weight_lbs=None, duration_sec=None) -> Set:
    if rep_count is not None:
        if weight_lbs is not None:
            return RepsAndWeightSet(rep_count=rep_count, weight_lbs=weight_lbs)
        elif duration_sec is not None:
            return RepsAndDurationSet(
                rep_count=rep_count,
                duration_sec=duration_sec,
            )
        else:
            return RepsOnlySet(rep_count=rep_count)
    elif duration_sec is not None:
        return DurationOnlySet(duration_sec=duration_sec)
    else:
        assert false, 'weight-only sets do not exist'

@dataclass
class Exercise:
    name: str
    sets: list[Set]

    @property
    def volume(self):
        if self.name == 'Pull Up': #lmao
            return 165 * sum(s.data.rep_count for s in self.sets)
        else:
            return sum(s.volume for s in self.sets)

    @property
    def note_format(self):
        return f'''
# [[{self.name}]]
{'\n'.join(f'{i+1}. {s.note_format}' for i, s in enumerate(self.sets))}'''

@dataclass
class Workout:
    title: str
    start_time: datetime
    duration: int # seconds
    exercises: dict[str, Exercise] # keyed on exercise name
    description: str

    @property
    def date_str(self):
        return self.start_time.strftime("%Y-%m-%d")

    @cached_property
    def volume(self):
        return sum(e.volume for e in self.exercises.values())

    @cached_property
    def note_format(self):
        return f'''---
title: "{self.title}"
tags:
  - "#workout"
topics:
  - "[[Gym]]"
workout: {'' if 'workout' in self.title else f'"[[{self.title}]]"'}
date: {self.date_str}
volume: {self.volume}
---
{'\n'.join(e.note_format for e in self.exercises.values())}
'''

    @property
    def filename(self):
        return f'{self.date_str} - {self.title} ({self.volume} lbs).md'

if __name__ == '__main__':
    app.run(debug=True, port=5000)
