from __future__ import annotations

from dataclasses import dataclass, field
import math
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
import time
from uuid import uuid4

from careerops.ai.orchestrator import AiOrchestrator
from careerops.core.repository import CareerOpsRepository


@dataclass
class _RunnerGeneration:
    number: int
    stop: Event
    queue: Queue[str]
    scheduled: set[str] = field(default_factory=set)
    scheduled_lock: Lock = field(default_factory=Lock)
    supervisor: Thread | None = None
    workers: list[Thread] = field(default_factory=list)

    @property
    def threads(self) -> list[Thread]:
        supervisor = [self.supervisor] if self.supervisor is not None else []
        return [*self.workers, *supervisor]

    def has_live_threads(self) -> bool:
        return any(thread.is_alive() for thread in self.threads)


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
        if (
            not isinstance(worker_count, int)
            or isinstance(worker_count, bool)
            or worker_count < 1
        ):
            raise ValueError("Task worker count must be a positive integer")
        self._repository = repository
        self._orchestrator = orchestrator
        self._poll_interval_seconds = interval
        self._worker_count = worker_count
        self._queue_size = max(worker_count * 4, 1)
        self._state_lock = Lock()
        self._generation: _RunnerGeneration | None = None
        self._next_generation = 1
        self.thread_name_prefix = f"careerops-ai-runner-{uuid4().hex[:10]}-"

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            generation = self._generation
            return generation is not None and self._is_generation_healthy(generation)

    def start(self) -> bool:
        with self._state_lock:
            current = self._generation
            if current is not None:
                if current.has_live_threads():
                    if self._is_generation_healthy(current):
                        return True
                    current.stop.set()
                    return False
                self._generation = None

            generation = _RunnerGeneration(
                number=self._next_generation,
                stop=Event(),
                queue=Queue(maxsize=self._queue_size),
            )
            self._next_generation += 1
            generation.workers = [
                Thread(
                    target=self._worker_loop,
                    args=(generation,),
                    name=(
                        f"{self.thread_name_prefix}worker-g{generation.number}-"
                        f"{index + 1}"
                    ),
                    daemon=True,
                )
                for index in range(self._worker_count)
            ]
            generation.supervisor = Thread(
                target=self._supervisor_loop,
                args=(generation,),
                name=f"{self.thread_name_prefix}supervisor-g{generation.number}",
                daemon=True,
            )
            self._generation = generation
            threads = generation.threads
            try:
                for thread in threads:
                    thread.start()
            except BaseException as exc:
                generation.stop.set()
                start_error: BaseException | None = exc
            else:
                start_error = None

        if start_error is not None:
            self._join_generation(generation)
            self._discard_drained_generation(generation)
            raise start_error

        if not self.is_running:
            generation.stop.set()
            self._join_generation(generation)
            self._discard_drained_generation(generation)
            return False
        return True

    def stop(self) -> None:
        with self._state_lock:
            generation = self._generation
            if generation is None:
                return
            generation.stop.set()

        self._join_generation(generation)
        self._discard_drained_generation(generation)

    def submit(self, task_id: str, *, allow_synchronous: bool = True) -> bool:
        with self._state_lock:
            generation = self._generation
            if generation is None:
                run_synchronously = allow_synchronous
            elif not generation.has_live_threads():
                self._generation = None
                generation = None
                run_synchronously = allow_synchronous
            elif not self._is_generation_healthy(generation):
                return False
            else:
                run_synchronously = False

        if generation is None and not run_synchronously:
            return False
        if run_synchronously:
            self._run_task(task_id)
            return True
        if generation is None:
            return False
        return self._submit_to_generation(generation, task_id)

    def _is_generation_healthy(self, generation: _RunnerGeneration) -> bool:
        return (
            not generation.stop.is_set()
            and len(generation.workers) == self._worker_count
            and all(worker.is_alive() for worker in generation.workers)
            and generation.supervisor is not None
            and generation.supervisor.is_alive()
        )

    def _join_generation(self, generation: _RunnerGeneration) -> None:
        timeout = max(1.0, self._poll_interval_seconds * 2)
        deadline = time.monotonic() + timeout
        for thread in generation.threads:
            if thread.ident is None:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def _discard_drained_generation(self, generation: _RunnerGeneration) -> None:
        with self._state_lock:
            if self._generation is generation and not generation.has_live_threads():
                self._generation = None

    def _submit_to_generation(
        self, generation: _RunnerGeneration, task_id: str
    ) -> bool:
        if generation.stop.is_set():
            return False
        with generation.scheduled_lock:
            if generation.stop.is_set() or task_id in generation.scheduled:
                return False
            generation.scheduled.add(task_id)
        try:
            generation.queue.put_nowait(task_id)
        except Full:
            with generation.scheduled_lock:
                generation.scheduled.discard(task_id)
            return False
        return True

    def _supervisor_loop(self, generation: _RunnerGeneration) -> None:
        while not generation.stop.is_set():
            self._poll_once(generation)
            generation.stop.wait(self._poll_interval_seconds)

    def _poll_once(self, generation: _RunnerGeneration) -> None:
        try:
            self._repository.requeue_interrupted_ai_tasks()
        except Exception:
            pass
        try:
            pending_ids = self._repository.list_pending_ai_task_ids(
                limit=generation.queue.maxsize
            )
        except Exception:
            return
        for task_id in pending_ids:
            if generation.stop.is_set():
                return
            self._submit_to_generation(generation, task_id)

    def _worker_loop(self, generation: _RunnerGeneration) -> None:
        while not generation.stop.is_set():
            try:
                task_id = generation.queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                self._run_task(task_id)
            finally:
                with generation.scheduled_lock:
                    generation.scheduled.discard(task_id)
                generation.queue.task_done()

    def _run_task(self, task_id: str) -> None:
        try:
            self._orchestrator.run(task_id)
        except Exception:
            # Claim failures leave the task pending for the next supervisor poll.
            return


__all__ = ["AiTaskRunner"]
