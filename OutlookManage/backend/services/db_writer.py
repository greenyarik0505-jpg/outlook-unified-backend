from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from backend.db import get_conn


@dataclass
class _Operation:
    db_path: Path
    callback: Callable[[Any], Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


_queue: queue.Queue[_Operation] = queue.Queue()


def _run_batch(operations: list[_Operation]) -> None:
    grouped: dict[Path, list[_Operation]] = {}
    for operation in operations:
        grouped.setdefault(operation.db_path, []).append(operation)
    for db_path, group in grouped.items():
        conn = None
        try:
            conn = get_conn(db_path)
            with conn:
                for index, operation in enumerate(group):
                    savepoint = f"writer_{index}"
                    try:
                        conn.execute(f"SAVEPOINT {savepoint}")
                        operation.result = operation.callback(conn)
                        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    except BaseException as exc:  # noqa: BLE001
                        operation.error = exc
                        try:
                            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                        except BaseException:
                            pass
                conn.commit()
        except BaseException as exc:  # noqa: BLE001
            for operation in group:
                if operation.error is None:
                    operation.error = exc
        finally:
            if conn is not None:
                conn.close()
            for operation in group:
                operation.done.set()
                _queue.task_done()


def _worker() -> None:
    while True:
        first = _queue.get()
        operations = [first]
        deadline = time.monotonic() + 0.005
        while len(operations) < 100 and time.monotonic() < deadline:
            try:
                operations.append(_queue.get_nowait())
            except queue.Empty:
                time.sleep(0.0005)
        _run_batch(operations)


threading.Thread(target=_worker, daemon=True, name="sqlite-writer").start()


def execute(db_path: Path, callback: Callable[[Any], Any]) -> Any:
    operation = _Operation(Path(db_path), callback)
    _queue.put(operation)
    operation.done.wait()
    if operation.error is not None:
        raise operation.error
    return operation.result


def flush() -> None:
    _queue.join()
