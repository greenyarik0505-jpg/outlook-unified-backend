from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from backend.db import get_conn
from backend.services import db_writer


class UnresolvedExportError(RuntimeError):
    def __init__(self, job_id: str):
        super().__init__(job_id)
        self.job_id = job_id


JOB_FIELDS = {
    "state",
    "phase",
    "requested_count",
    "domain",
    "min_registered_days",
    "retest",
    "concurrency",
    "total",
    "cancel_requested",
    "filename",
    "file_path",
    "file_count",
    "file_parts",
    "export_outlook",
    "export_hotmail",
    "checksum",
    "downloaded_at",
    "expires_at",
    "deleted_at",
    "error",
    "updated_at",
    "finished_at",
}

ITEM_FIELDS = {
    "email",
    "domain",
    "ordinal",
    "test_state",
    "delete_state",
    "reason",
    "remote_status",
    "updated_at",
}


def create_job(
    db_path: Path,
    job: dict[str, Any],
    items: Iterable[dict[str, Any]],
    *,
    require_no_unresolved: bool = False,
) -> None:
    item_list = list(items)

    def write(conn):
        if require_no_unresolved:
            unresolved = conn.execute(
                "SELECT id FROM export_jobs "
                "WHERE state='running' OR phase IN ('partial_ready','delete_interrupted') "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            if unresolved:
                raise UnresolvedExportError(str(unresolved["id"]))
        conn.execute(
            """
            INSERT INTO export_jobs (
                id,state,phase,requested_count,domain,min_registered_days,retest,concurrency,total,
                cancel_requested,filename,file_parts,started_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job["id"],
                job.get("state", "running"),
                job.get("phase", "queued"),
                int(job.get("requested_count", 0)),
                job.get("domain", "all"),
                int(job.get("min_registered_days", 0)),
                1 if job.get("retest", True) else 0,
                int(job.get("concurrency", 1)),
                int(job.get("total", 0)),
                0,
                job.get("filename", ""),
                int(job.get("file_parts", 1)),
                job["started_at"],
                job["updated_at"],
            ),
        )
        conn.executemany(
            """
            INSERT INTO export_items (
                job_id,account_id,email,domain,ordinal,test_state,delete_state,reason,remote_status,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    job["id"],
                    int(item["account_id"]),
                    item.get("email", ""),
                    item.get("domain", ""),
                    int(item.get("ordinal", 0)),
                    item.get("test_state", "pending"),
                    item.get("delete_state", "pending"),
                    item.get("reason", ""),
                    item.get("remote_status", ""),
                    job["started_at"],
                    job["updated_at"],
                )
                for item in item_list
            ],
        )
    db_writer.execute(db_path, write)


def get_job(db_path: Path, job_id: str) -> dict[str, Any] | None:
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM export_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(db_path: Path, limit: int = 20) -> list[dict[str, Any]]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM export_jobs ORDER BY updated_at DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
    return [dict(row) for row in rows]


def update_job(db_path: Path, job_id: str, **fields: Any) -> None:
    values = {key: value for key, value in fields.items() if key in JOB_FIELDS}
    if not values:
        return
    assignments = ",".join(f"{key}=?" for key in values)
    def write(conn):
        conn.execute(
            f"UPDATE export_jobs SET {assignments} WHERE id=?",
            (*values.values(), job_id),
        )
    db_writer.execute(db_path, write)


def claim_job_phase(
    db_path: Path,
    job_id: str,
    expected_phases: tuple[str, ...],
    **fields: Any,
) -> bool:
    values = {key: value for key, value in fields.items() if key in JOB_FIELDS}
    if not values or not expected_phases:
        return False
    assignments = ",".join(f"{key}=?" for key in values)
    marks = ",".join("?" for _ in expected_phases)

    def write(conn):
        cursor = conn.execute(
            f"UPDATE export_jobs SET {assignments} "
            f"WHERE id=? AND phase IN ({marks}) AND state!='running'",
            (*values.values(), job_id, *expected_phases),
        )
        return cursor.rowcount == 1

    return bool(db_writer.execute(db_path, write))


def update_item(db_path: Path, job_id: str, account_id: int, **fields: Any) -> None:
    values = {key: value for key, value in fields.items() if key in ITEM_FIELDS}
    if not values:
        return
    assignments = ",".join(f"{key}=?" for key in values)
    def write(conn):
        conn.execute(
            f"UPDATE export_items SET {assignments} WHERE job_id=? AND account_id=?",
            (*values.values(), job_id, int(account_id)),
        )
    db_writer.execute(db_path, write)


def mark_pending_unused(db_path: Path, job_id: str, updated_at: str) -> None:
    def write(conn):
        conn.execute(
            "UPDATE export_items SET test_state='unused',reason='配额已满足',updated_at=? "
            "WHERE job_id=? AND test_state='pending'",
            (updated_at, job_id),
        )
    db_writer.execute(db_path, write)


def get_items(
    db_path: Path,
    job_id: str,
    *,
    test_state: str | None = None,
    delete_states: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    clauses = ["job_id=?"]
    params: list[Any] = [job_id]
    if test_state is not None:
        clauses.append("test_state=?")
        params.append(test_state)
    if delete_states:
        marks = ",".join("?" for _ in delete_states)
        clauses.append(f"delete_state IN ({marks})")
        params.extend(delete_states)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM export_items WHERE {' AND '.join(clauses)} ORDER BY ordinal",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def counts(db_path: Path, job_id: str) -> dict[str, int]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT test_state, delete_state, COUNT(*) AS n FROM export_items WHERE job_id=? "
            "GROUP BY test_state, delete_state",
            (job_id,),
        ).fetchall()
    out = {
        "total": 0,
        "pending": 0,
        "tested_ok": 0,
        "abused": 0,
        "test_failed": 0,
        "not_alive": 0,
        "unused": 0,
        "deleted": 0,
    }
    for row in rows:
        n = int(row["n"])
        out["total"] += n
        state = str(row["test_state"] or "pending")
        if state in out:
            out[state] += n
        if row["delete_state"] == "deleted":
            out["deleted"] += n
    out["processed"] = out["total"] - out["pending"]
    return out


def reason_summary(db_path: Path, job_id: str) -> dict[str, dict[str, int]]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT test_state, COALESCE(NULLIF(reason, ''), test_state) AS reason, COUNT(*) AS n
            FROM export_items
            WHERE job_id=? AND test_state!='pending'
            GROUP BY test_state, COALESCE(NULLIF(reason, ''), test_state)
            ORDER BY n DESC
            """,
            (job_id,),
        ).fetchall()
    result: dict[str, dict[str, int]] = {"ok": {}, "fail": {}, "skip": {}}
    for row in rows:
        state = row["test_state"]
        bucket = "ok" if state == "tested_ok" else ("fail" if state == "test_failed" else "skip")
        result[bucket][str(row["reason"])] = int(row["n"])
    return result


def remote_failed_count(db_path: Path, job_id: str) -> int:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM export_items WHERE job_id=? AND remote_status LIKE 'failed:%'",
            (job_id,),
        ).fetchone()
    return int(row[0])


def delete_local_account(
    db_path: Path,
    job_id: str,
    account_id: int,
    remote_status: str,
    updated_at: str,
) -> None:
    def write(conn):
        conn.execute("DELETE FROM accounts WHERE id=?", (int(account_id),))
        conn.execute("DELETE FROM history WHERE account_id=?", (int(account_id),))
        conn.execute(
            """
            UPDATE export_items
            SET delete_state='deleted', remote_status=?, updated_at=?
            WHERE job_id=? AND account_id=?
            """,
            (remote_status, updated_at, job_id, int(account_id)),
        )
    db_writer.execute(db_path, write)


def job_view(db_path: Path, job: dict[str, Any]) -> dict[str, Any]:
    summary = counts(db_path, job["id"])
    skipped = summary["abused"] + summary["not_alive"] + summary["unused"]
    phase = str(job.get("phase") or "")
    state = str(job.get("state") or "cancelled")
    requested = int(job.get("requested_count") or 0)
    tested = (
        summary["tested_ok"]
        + summary["test_failed"]
        + summary["abused"]
        + summary["not_alive"]
    )
    remaining = max(0, requested - summary["tested_ok"])
    return {
        "id": job["id"],
        "type": "export",
        "state": state,
        "cancelled": bool(job.get("cancel_requested")) or state == "cancelled",
        "total": summary["total"],
        "processed": summary["processed"],
        "succeeded": summary["tested_ok"],
        "failed": summary["test_failed"],
        "skipped": skipped,
        "export_pending": summary["pending"],
        "export_requested_count": requested,
        "export_tested_count": tested,
        "export_remaining": remaining,
        "export_reserve_pending": summary["pending"],
        "export_abused": summary["abused"],
        "export_not_alive": summary["not_alive"],
        "export_unused": summary["unused"],
        "export_deleted": summary["deleted"],
        "export_phase": phase,
        "export_partial_ready": phase == "partial_ready" and summary["tested_ok"] > 0,
        "export_partial_count": summary["tested_ok"],
        "export_resume_ready": phase == "delete_interrupted",
        "export_ready": state == "done" and bool(job.get("file_path")),
        "export_filename": job.get("filename") or "",
        "export_count": int(job.get("file_count") or 0),
        "export_file_parts": max(1, int(job.get("file_parts") or 1)),
        "export_outlook": int(job.get("export_outlook") or 0),
        "export_hotmail": int(job.get("export_hotmail") or 0),
        "export_remote_failed": remote_failed_count(db_path, job["id"]),
        "export_checksum": job.get("checksum") or "",
        "export_downloaded_at": job.get("downloaded_at") or "",
        "export_expires_at": job.get("expires_at") or "",
        "export_deleted_at": job.get("deleted_at") or "",
        "concurrency": int(job.get("concurrency") or 1),
        "error": job.get("error") or "",
        "started_at": job.get("started_at") or "",
        "finished_at": job.get("finished_at") or "",
        "reasons": reason_summary(db_path, job["id"]),
        "items": [],
    }
