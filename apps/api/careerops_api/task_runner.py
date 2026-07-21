from __future__ import annotations

import math
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from uuid import uuid4

from careerops.ai.orchestrator import AiOrchestrator
from careerops.core.repository import CareerOpsRepository


class AiTaskRunner:
    def __init__(
        self,
        repository: CareerOpsRepository,
        orchestrator: AiOrchestrator,
        *,
        poll_interval_seconds: float = 1.0,
        worker_count: int = 2,
    ) -> None:
        interval = float(poll_interval_seconds)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Task poll interval must be finite and positive")
        if not isinstance(worker_count, int) or isinstance(worker_count, bool) or worker_count < 1:
            raise ValueError("Task worker count must be a positive integer")
        self._repository = repository
        self._orchestrator = orchestrator
        self._poll_interval_seconds = interval
        self._worker_count = worker_count
        self._queue: Queue[str | None] = Queue(maxsize=max(worker_count * 4, 1))
        self._scheduled: set[str] = set()
        self._scheduled_lock = Lock()
        self._state_lock = Lock()
        self._stop = Event()
        self._running = False
        self._supervisor: Thread | None = None
        self._workers: list[Thread] = []
        self.thread_name_prefix = f"careerops-ai-runner-{uuid4().hex[:10]}-"

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    def start(self) -> None:
        with self._state_lock:
            if self._running:
                return
            self._stop.clear()
            self._running = True
            self._workers = [
                Thread(
                    target=self._worker_loop,
                    name=f"{self.thread_name_prefix}worker-{index + 1}",
                    daemon=True,
                )
                for index in range(self._worker_count)
            ]
            self._supervisor = Thread(
                target=self._supervisor_loop,
                name=f"{self.thread_name_prefix}supervisor",
                daemon=True,
            )
            threads = [*self._workers, self._supervisor]
        for thread in threads:
            thread.start()

    def stop(self) -> None:
        with self._state_lock:
            if not self._running:
                return
            self._running = False
            self._stop.set()
            supervisor = self._supervisor
            workers = list(self._workers)
        if supervisor is not None:
            supervisor.join(timeout=max(1.0, self._poll_interval_seconds * 2))
        for _worker in workers:
            try:
                self._queue.put_nowait(None)
            except Full:
                break
        for worker in workers:
            worker.join(timeout=1.0)
        with self._scheduled_lock:
            self._scheduled.clear()
        with self._state_lock:
            self._supervisor = None
            self._workers = []

    def submit(self, task_id: str) -> bool:
        if not self.is_running:
            self._run_task(task_id)
            return True
        with self._scheduled_lock:
            if task_id in self._scheduled:
                return False
            self._scheduled.add(task_id)
        try:
            self._queue.put_nowait(task_id)
        except Full:
            with self._scheduled_lock:
                self._scheduled.discard(task_id)
            return False
        return True

    def _supervisor_loop(self) -> None:
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(self._poll_interval_seconds)

    def _poll_once(self) -> None:
        try:
            self._repository.requeue_interrupted_ai_tasks()
        except Exception:
            pass
        try:
            pending_ids = self._repository.list_pending_ai_task_ids(
                limit=self._queue.maxsize
            )
        except Exception:
            return
        for task_id in pending_ids:
            if self._stop.is_set():
                return
            self.submit(task_id)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                task_id = self._queue.get(timeout=0.1)
            except Empty:
                continue
            if task_id is None:
                self._queue.task_done()
                return
            try:
                self._run_task(task_id)
            finally:
                with self._scheduled_lock:
                    self._scheduled.discard(task_id)
                self._queue.task_done()

    def _run_task(self, task_id: str) -> None:
        try:
            self._orchestrator.run(task_id)
        except Exception:
            # Claim failures leave the task pending for the next supervisor poll.
            return


__all__ = ["AiTaskRunner"]
