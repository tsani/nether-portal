from dataclasses import dataclass
from enum import StrEnum, auto
from functools import cached_property
from datetime import datetime

KG_TO_LBS = 2.20462

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

def make_set_data(rep_count=None, weight_lbs=None, duration_sec=None) -> SetData:
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
        assert False, 'weight-only sets do not exist'

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
daily_note: [[{self.date_str}]]
volume: {self.volume}
---
{'\n'.join(e.note_format for e in self.exercises.values())}
'''

    @property
    def filename(self):
        return f'{self.date_str} - {self.title} ({self.volume} lbs).md'
