"""后台批量任务执行器：提交一批账号 + worker 函数，立即返回 job_id，前端轮询进度。

取消语义：硬取消 —— 停投递、取消未开始 future、杀协议子进程、立即标 cancelled，
在途 worker 若稍后返回则记为 skip，且 worker 内应检查 is_cancelled 避免写库。
"""
from __future__ import annotations

import copy
import inspect
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any, Callable

from backend.services import job_store, protocols

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_counter = count(1)
MAX_FINISHED_JOBS = 40
MAX_GLOBAL_WORKERS = 100

# job_id -> executor / futures（用于硬取消）
_executors: dict[str, ThreadPoolExecutor] = {}
_futures: dict[str, set[Future]] = {}
_active_job_threads: set[str] = set()
_runtime_lock = threading.Lock()
_db_path: Path | None = None
_worker_slots = threading.BoundedSemaphore(MAX_GLOBAL_WORKERS)


@dataclass(frozen=True)
class TaskContext:
    job_id: str

    def is_cancelled(self) -> bool:
        return is_cancelled(self.job_id)

    def checkpoint(self) -> None:
        if self.is_cancelled():
            raise TaskCancelled("任务已取消")


class TaskCancelled(RuntimeError):
    pass


def _snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(job)


@contextmanager
def worker_slot(is_cancelled: Callable[[], bool]):
    acquired = False
    try:
        while not acquired:
            if is_cancelled():
                raise TaskCancelled("任务已取消")
            acquired = _worker_slots.acquire(timeout=0.2)
        if is_cancelled():
            raise TaskCancelled("任务已取消")
        yield
    finally:
        if acquired:
            _worker_slots.release()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def configure(db_path: Path) -> None:
    global _db_path
    _db_path = Path(db_path)
    job_store.mark_interrupted(_db_path, _now())
    with _jobs_lock:
        for job in job_store.load(_db_path, MAX_FINISHED_JOBS):
            if job.get("type") != "export":
                _jobs[job["id"]] = job


def _persist(job: dict[str, Any] | None) -> None:
    if _db_path is not None and job:
        job_store.save(_db_path, job)


def create_job(job_type: str, total: int, *, job_id: str | None = None, **extra: Any) -> str:
    job_id = job_id or f"{job_type}-{next(_counter)}-{time.time_ns()}"
    with _jobs_lock:
        job: dict[str, Any] = {
            "id": job_id,
            "type": job_type,
            "total": total,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "state": "running",
            "cancelled": False,
            "error": "",
            "started_at": _now(),
            "finished_at": "",
            "updated_at": _now(),
            "items": [],
            "reasons": {},
        }
        if extra:
            job.update(extra)
        _jobs[job_id] = job
        _prune_finished_jobs_locked()
        snapshot = _snapshot(job)
    _persist(snapshot)
    return job_id


def update_job(job_id: str, **fields: Any) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = _now()
        snapshot = _snapshot(job)
    _persist(snapshot)


def _prune_finished_jobs_locked() -> None:
    finished = [j for j in _jobs.values() if j.get("state") != "running"]
    if len(finished) <= MAX_FINISHED_JOBS:
        return
    finished.sort(key=lambda j: j.get("finished_at") or j.get("started_at") or "")
    for job in finished[: max(0, len(finished) - MAX_FINISHED_JOBS)]:
        _jobs.pop(job["id"], None)


def _record(job_id: str, result: dict[str, Any]) -> None:
    status = result.get("status", "fail")
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        # 取消后已把剩余项 bulk 计为 skip，在途晚到的结果不再改动计数
        if job.get("cancelled") and int(job.get("processed", 0)) >= int(job.get("total", 0)):
            return
        # 已取消后仍返回的结果：统一记 skip，不污染成功/失败
        if job.get("cancelled") and status != "skip":
            status = "skip"
            result = {
                **result,
                "status": "skip",
                "reason": result.get("reason") or "任务已取消（在途中止）",
            }
        job["processed"] += 1
        if status == "ok":
            job["succeeded"] += 1
        elif status == "skip":
            job["skipped"] += 1
        else:
            job["failed"] += 1
        reason = str(result.get("reason") or "未知").strip()[:160] or "未知"
        bucket = job["reasons"].setdefault(status, {})
        bucket[reason] = bucket.get(reason, 0) + 1
        job["items"].append(result)
        if len(job["items"]) > 500:
            job["items"] = job["items"][-300:]
        job["updated_at"] = _now()
        snapshot = _snapshot(job)
    _persist(snapshot)


def _finish(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            if job.get("cancelled"):
                job["state"] = "cancelled"
            elif job.get("error"):
                job["state"] = "failed"
            else:
                job["state"] = "done"
            if not job.get("finished_at"):
                job["finished_at"] = _now()
            job["updated_at"] = _now()
            _prune_finished_jobs_locked()
            snapshot = _snapshot(job)
        else:
            snapshot = None
    _persist(snapshot)
    with _runtime_lock:
        _executors.pop(job_id, None)
        _futures.pop(job_id, None)


def is_cancelled(job_id: str) -> bool:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return bool(job and job.get("cancelled"))


def is_active(job_id: str) -> bool:
    with _runtime_lock:
        return job_id in _active_job_threads


def _register_future(job_id: str, fut: Future) -> None:
    with _runtime_lock:
        _futures.setdefault(job_id, set()).add(fut)

    def _done(f: Future) -> None:
        with _runtime_lock:
            s = _futures.get(job_id)
            if s is not None:
                s.discard(f)

    fut.add_done_callback(_done)


def _worker_mode(worker: Callable[..., Any]) -> str:
    try:
        params = inspect.signature(worker).parameters
    except (TypeError, ValueError):
        return "plain"
    if "task" in params:
        return "task"
    if "job_id" in params:
        return "job_id"
    return "plain"


def submit(job_type: str, items: list[Any], worker: Callable[[Any], dict[str, Any]], max_workers: int = 4) -> str:
    job_id = create_job(job_type, len(items))
    total = len(items)
    worker_mode = _worker_mode(worker)

    def runner() -> None:
        try:
            item_iter = iter(items)
            pending: set[Future] = set()
            workers = max(1, max_workers)
            pool = ThreadPoolExecutor(max_workers=workers)
            with _runtime_lock:
                _executors[job_id] = pool

            def submit_next() -> bool:
                if is_cancelled(job_id):
                    return False
                try:
                    item = next(item_iter)
                except StopIteration:
                    return False
                fut = pool.submit(_safe_worker, worker, item, job_id, worker_mode)
                pending.add(fut)
                _register_future(job_id, fut)
                return True

            try:
                for _ in range(workers):
                    if not submit_next():
                        break
                while pending:
                    if is_cancelled(job_id):
                        # 硬取消：不再等待在途结果；cancel 未开始的 future
                        for fut in list(pending):
                            fut.cancel()
                        pending.clear()
                        break
                    done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                    if not done:
                        continue
                    for fut in done:
                        if fut.cancelled():
                            _record(job_id, {"status": "skip", "reason": "任务已取消"})
                            continue
                        try:
                            _record(job_id, fut.result())
                        except Exception as exc:  # noqa: BLE001
                            _record(job_id, {"status": "fail", "reason": str(exc)})
                    if is_cancelled(job_id):
                        continue
                    for _ in range(len(done)):
                        if not submit_next():
                            break
            finally:
                # 取消兜底：cancel() 已 bulk skip；若未走 cancel API 仅设 flag，这里补齐
                if is_cancelled(job_id):
                    with _jobs_lock:
                        job = _jobs.get(job_id)
                        if job:
                            _mark_remaining_skipped_locked(job)
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    pool.shutdown(wait=False)
                except Exception:
                    pass
        finally:
            _finish(job_id)
            with _runtime_lock:
                _active_job_threads.discard(job_id)

    with _runtime_lock:
        _active_job_threads.add(job_id)
    threading.Thread(target=runner, daemon=True, name=f"job-{job_id}").start()
    return job_id


def _safe_worker(
    worker: Callable[[Any], dict[str, Any]],
    item: Any,
    job_id: str,
    worker_mode: str,
) -> dict[str, Any]:
    if is_cancelled(job_id):
        return {
            "status": "skip",
            "account_id": item if isinstance(item, int) else None,
            "reason": "任务已取消",
        }
    try:
        with worker_slot(lambda: is_cancelled(job_id)):
            if worker_mode == "task":
                return worker(item, task=TaskContext(job_id))
            if worker_mode == "job_id":
                return worker(item, job_id=job_id)
            return worker(item)
    except TaskCancelled as exc:
        return {"status": "skip", "account_id": item if isinstance(item, int) else None, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001
        if is_cancelled(job_id):
            return {
                "status": "skip",
                "account_id": item if isinstance(item, int) else None,
                "reason": "任务已取消",
            }
        return {
            "status": "fail",
            "account_id": item if isinstance(item, int) else None,
            "reason": str(exc),
        }


def submit_custom(
    job_type: str,
    total: int,
    runner: Callable[..., None],
    *,
    job_extra: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> str:
    job_id = create_job(job_type, total, job_id=job_id, **(job_extra or {}))
    cleanups: list[Callable[[], Any]] = []

    def progress(status: str = "ok", **extra: Any) -> None:
        if is_cancelled(job_id) and status != "skip":
            status = "skip"
            extra = {**extra, "reason": extra.get("reason") or "任务已取消"}
        _record(job_id, {"status": status, **extra})

    def set_total(value: int) -> None:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job:
                job["total"] = max(0, int(value))
                job["updated_at"] = _now()
                snapshot = _snapshot(job)
            else:
                snapshot = None
        _persist(snapshot)

    def add_cleanup(callback: Callable[[], Any]) -> None:
        cleanups.append(callback)

    progress.set_total = set_total
    progress.add_cleanup = add_cleanup
    progress.job_id = job_id

    def wrapped() -> None:
        try:
            cancel_check = lambda: is_cancelled(job_id)
            try:
                param_count = len(inspect.signature(runner).parameters)
            except (TypeError, ValueError):
                param_count = 1
            if param_count >= 2:
                runner(progress, cancel_check)
            else:
                runner(progress)
        except Exception as exc:  # noqa: BLE001
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job and not job.get("cancelled"):
                    job["error"] = str(exc)
        finally:
            for cleanup in reversed(cleanups):
                try:
                    cleanup()
                except Exception:
                    pass
            _finish(job_id)
            with _runtime_lock:
                _active_job_threads.discard(job_id)

    with _runtime_lock:
        _active_job_threads.add(job_id)
    threading.Thread(target=wrapped, daemon=True, name=f"job-{job_id}").start()
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return _snapshot(job) if job else None


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    with _jobs_lock:
        current_jobs = [_snapshot(job) for job in _jobs.values()]
    current_jobs.sort(key=lambda j: j["started_at"], reverse=True)
    summaries = []
    for job in current_jobs[:limit]:
        summary = {k: v for k, v in job.items() if k != "items"}
        summaries.append(summary)
    return summaries


def _mark_remaining_skipped_locked(job: dict[str, Any]) -> None:
    remaining = max(0, int(job.get("total", 0)) - int(job.get("processed", 0)))
    if remaining <= 0:
        return
    job["skipped"] += remaining
    job["processed"] = int(job.get("total", 0))
    bucket = job["reasons"].setdefault("skip", {})
    bucket["任务已取消"] = bucket.get("任务已取消", 0) + remaining


def cancel(job_id: str) -> bool:
    """硬取消：立即标 cancelled、杀协议子进程、shutdown 线程池、cancel futures。"""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job["state"] != "running":
            return False
        job["cancelled"] = True
        # 立刻让前端看到 cancelled（不等 runner finally）
        job["state"] = "cancelled"
        job["finished_at"] = _now()
        # 未处理项立即记 skip，前端轮询立刻看到完整计数
        _mark_remaining_skipped_locked(job)
        job["updated_at"] = _now()
        snapshot = _snapshot(job)

    _persist(snapshot)

    # 杀协议子进程（硬中断网络/探测）
    try:
        protocols.kill_job_procs(job_id)
    except Exception:
        pass

    with _runtime_lock:
        pool = _executors.pop(job_id, None)
        futs = list(_futures.pop(job_id, set()) or [])

    for fut in futs:
        try:
            fut.cancel()
        except Exception:
            pass

    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            try:
                pool.shutdown(wait=False)
            except Exception:
                pass
        except Exception:
            pass

    return True
