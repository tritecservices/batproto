"""Long-running work (import a card, detection, reports) off the UI thread, with progress.

Each task runs in a background thread and collects its progress messages. The app
polls ``/api/tasks/<id>``. Percentages come from the engine's own progress lines
("encoding  42.0%"), so the bar moves while a review copy is being made.
"""
from __future__ import annotations

import re
import threading
import time
import traceback
import uuid
from typing import Any, Callable

PCT = re.compile(r"(\d{1,3}(?:\.\d)?)%")


class Task:
    def __init__(self, title: str, steps: int = 1):
        self.id = uuid.uuid4().hex[:12]
        self.title = title
        self.state = "running"                  # running | done | failed
        self.lines: list[str] = []
        self.step, self.steps = -1, max(1, steps)
        self.percent: float = 0.0
        self.stage = ""                         # plain-English label for the current step
        self.stage_pct: float | None = None
        self.result: Any = None
        self.error: str | None = None
        self.started = time.time()
        self._lock = threading.Lock()

    def say(self, msg: str) -> None:
        msg = str(msg).rstrip()
        if not msg:
            return
        with self._lock:
            self.lines.append(msg)
            del self.lines[:-200]
            m = PCT.search(msg)
            if m:
                inner = min(100.0, float(m.group(1))) / 100
                self.stage_pct = round(inner * 100)
                self.percent = round(100 * (max(self.step, 0) + inner) / self.steps, 1)

    def next_step(self, label: str) -> None:
        with self._lock:
            self.step = min(self.step + 1, self.steps - 1)
            self.percent = round(100 * self.step / self.steps, 1)
            self.stage, self.stage_pct = label, None
        self.say(label)

    def view(self) -> dict:
        with self._lock:
            return {"id": self.id, "title": self.title, "state": self.state,
                    "percent": 100.0 if self.state == "done" else self.percent,
                    "last": self.lines[-1] if self.lines else "", "lines": self.lines[-30:],
                    "stage": self.stage + (f" · {self.stage_pct:.0f}%" if self.stage_pct is not None else ""),
                    "result": self.result, "error": self.error,
                    "seconds": round(time.time() - self.started)}


class Tasks:
    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    def start(self, title: str, fn: Callable[[Task], Any], steps: int = 1) -> Task:
        task = Task(title, steps)
        with self._lock:
            self._tasks[task.id] = task

        def run():
            try:
                task.result = fn(task)
                task.state = "done"
            except Exception as exc:              # noqa: BLE001 - shown to the user
                task.error = str(exc) or exc.__class__.__name__
                task.say(traceback.format_exc(limit=3))
                task.state = "failed"
        threading.Thread(target=run, daemon=True, name=f"task-{task.id}").start()
        return task

    def get(self, tid: str) -> Task | None:
        return self._tasks.get(tid)

    def busy(self) -> bool:
        return any(t.state == "running" for t in self._tasks.values())
