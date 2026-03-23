import sys
from functools import cached_property
from datetime import datetime
from dataclasses import dataclass, fields
from enum import StrEnum, auto
import csv

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

@dataclass
class Record:
    title: str
    start_time_str: str
    end_time_str: str
    description: str | None
    superset_id: str | None
    exercise_name: str
    exercise_notes: str | None
    set_index: int
    set_kind: str
    weight_lbs: float | None
    rep_count: int | None
    distance_km: float | None
    duration_sec: int | None
    rpe: int | None

    @staticmethod
    def from_row(row):
        return Record(
            title=row[0],
            start_time_str=row[1],
            end_time_str=row[2],
            description=row[3] or None,
            exercise_name=row[4],
            superset_id=row[5] or None,
            exercise_notes=row[6] or None,
            set_index=row[7],
            set_kind=row[8],
            weight_lbs=float(row[9]) if row[9] else None,
            rep_count=int(row[10]) if row[10] else None,
            distance_km=float(row[11]) if row[11] else None,
            duration_sec=int(row[12]) if row[12] else None,
            rpe=int(row[13]) if row[13] else None,
        )

def hevy_csv_records(hevy_file):
    return (Record.from_row(row) for row in csv.reader(hevy_file))

def group_workouts_from_records(records):
    workouts = {} # keyed on start time string
    for r in records:
        w = workouts.get(r.start_time_str, None)
        if w is None:
            start_time = datetime.strptime(r.start_time_str, "%d %b %Y, %H:%M")
            end_time = datetime.strptime(r.end_time_str, "%d %b %Y, %H:%M")
            w = Workout(
                title=r.title,
                start_time=start_time,
                duration=(end_time - start_time).total_seconds(),
                exercises={},
                description=r.description,
            )
            workouts[r.start_time_str] = w
        e = w.exercises.get(r.exercise_name, None)
        if e is None:
            e = Exercise(name=r.exercise_name, sets=[])
            w.exercises[e.name] = e
        e.sets.append(
            Set(
                data=make_set_data(
                    rep_count=r.rep_count,
                    weight_lbs=r.weight_lbs,
                    duration_sec=r.duration_sec,
                ),
                kind=SetKind(r.set_kind),
            ),
        )
    return workouts

def main(hevy_file):
    next(hevy_file) # skip header
    workouts = group_workouts_from_records(hevy_csv_records(hevy_file))

    VAULT_DIR = '/home/tsani/personal-vault/workouts'
    for w in workouts.values():
        with open(f'{VAULT_DIR}/{w.filename}', 'w') as f:
            f.write(w.note_format)

if __name__ == '__main__':
    with open ('/home/tsani/Downloads/workouts.csv') as f:
        main(f)
