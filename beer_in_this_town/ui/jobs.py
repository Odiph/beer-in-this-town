"""One long-running thing at a time, with a log the page can read.

The dashboard's job is to make waiting legible. A sign-in can take a person
several minutes, a selector check makes two paced requests, and a headless
verification starts a browser -- all of which look identical from the outside
(nothing happening) unless something says otherwise.

So every slow action runs here, as a `Job` that appends plain-English steps as
it goes. The page polls and renders them. The rule for what goes in a step is
the same one the CLI uses for its logs: could a person tell, from this alone,
what the tool is doing and why it is taking this long?

**One job at a time, deliberately.** Two sign-ins racing for the same Chrome
profile is how a profile gets corrupted, and the repo's own guardrails already
refuse concurrent writes for the same reason. `start` returns None rather than
queueing, and the caller reports that as "something is already running".
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace

log = logging.getLogger(__name__)

RUNNING, DONE, FAILED = "running", "done", "failed"


@dataclass(frozen=True)
class Step:
    """One line of the narration, with when it happened."""

    at: float
    text: str
    # A step that explains a wait rather than reporting progress. The page
    # renders it quieter, so a pacing note does not read like an event.
    aside: bool = False

    def to_row(self) -> dict:
        return {"at": self.at, "text": self.text, "aside": self.aside}


@dataclass(frozen=True)
class Job:
    """An immutable snapshot of one running action."""

    id: str
    kind: str
    title: str
    state: str
    steps: tuple[Step, ...] = ()
    started: float = 0.0
    ended: float | None = None
    result: dict = field(default_factory=dict)
    error: str = ""

    @property
    def elapsed(self) -> float:
        return (self.ended or time.time()) - self.started

    def to_row(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "title": self.title,
            "state": self.state, "steps": [s.to_row() for s in self.steps],
            "elapsed": round(self.elapsed, 1), "result": self.result,
            "error": self.error,
        }


class Runner:
    """Holds the one current job and serialises access to it.

    Every mutation replaces the snapshot rather than editing it, so a reader
    holding `runner.current` keeps a consistent view even mid-write -- which
    matters here because the page polls from another thread on every request.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: Job | None = None
        self._thread: threading.Thread | None = None

    @property
    def current(self) -> Job | None:
        with self._lock:
            return self._job

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._job is not None and self._job.state == RUNNING

    def start(self, kind: str, title: str,
              work: Callable[[Callable[[str], None]], dict]) -> Job | None:
        """Run `work` on a thread, handing it a `say` callback for narration.

        Returns None when something is already running. Not a queue: see the
        module docstring on why a second sign-in must not start.
        """
        with self._lock:
            if self._job is not None and self._job.state == RUNNING:
                return None
            job = Job(id=uuid.uuid4().hex[:8], kind=kind, title=title,
                      state=RUNNING, started=time.time())
            self._job = job

        def say(text: str, aside: bool = False) -> None:
            self._append(job.id, Step(time.time(), text, aside))

        def run() -> None:
            try:
                result = work(say)
            except Exception as exc:
                log.error("job %s (%s) failed: %s", job.id, kind, exc)
                self._finish(job.id, FAILED, {}, f"{type(exc).__name__}: {exc}")
                return
            self._finish(job.id, DONE, result or {}, "")

        thread = threading.Thread(target=run, name=f"beertown-ui-{kind}",
                                  daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        return job

    def _append(self, job_id: str, step: Step) -> None:
        with self._lock:
            if self._job is None or self._job.id != job_id:
                return  # a stale thread from a job that has been replaced
            self._job = replace(self._job, steps=(*self._job.steps, step))
        log.info("[ui] %s", step.text)

    def _finish(self, job_id: str, state: str, result: dict,
                error: str) -> None:
        with self._lock:
            if self._job is None or self._job.id != job_id:
                return
            self._job = replace(self._job, state=state, ended=time.time(),
                                result=result, error=error)
