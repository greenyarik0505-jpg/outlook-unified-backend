from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.db import get_conn
from backend.services import db_writer


def save(db_path: Path, job: dict[str, Any]) -> None:
    payload = json.dumps(job, ensure_ascii=False, separators=(",", ":"))

    def write(conn):
        conn.execute(
            """
            INSERT INTO background_jobs (id,type,state,payload,started_at,updated_at,finished_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                type=excluded.type,state=excluded.state,payload=excluded.payload,
                updated_at=excluded.updated_at,finished_at=excluded.finished_at
            """,
            (
                job["id"],
                job.get("type", ""),
                job.get("state", ""),
                payload,
                job.get("started_at", ""),
                job.get("updated_at") or job.get("finished_at") or job.get("started_at", ""),
                job.get("finished_at", ""),
            ),
        )

    db_writer.execute(db_path, write)


def load(db_path: Path, limit: int = 40) -> list[dict[str, Any]]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT payload FROM background_jobs ORDER BY updated_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    jobs = []
    for row in rows:
        try:
            value = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("id"):
            jobs.append(value)
    return jobs


def mark_interrupted(db_path: Path, finished_at: str) -> None:
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT id,payload FROM background_jobs WHERE state='running'").fetchall()
    for row in rows:
        try:
            job = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            job = {"id": row["id"], "type": "unknown"}
        job.update(
            state="failed",
            error="后端进程中断，任务执行状态已恢复为失败",
            finished_at=finished_at,
            updated_at=finished_at,
        )
        save(db_path, job)
