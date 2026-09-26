import json
import gc
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db import get_conn, init_db
from backend.services import db_writer, export_store, job_store, jobs, remote_pool
from backend.services.runtime_lease import RuntimeLease


class TempMainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.db = self.root / "accounts.db"
        init_db(self.db)
        import backend.main as main
        self.main = main
        self.old_db = main.DB_PATH
        self.old_exports = main.EXPORT_DIR
        self.old_log = main.LOG_PATH
        main.DB_PATH = self.db
        main.EXPORT_DIR = self.root / "exports"
        main.LOG_PATH = self.root / "app.log"

    def tearDown(self):
        self.main.flush_logs()
        self.main.DB_PATH = self.old_db
        self.main.EXPORT_DIR = self.old_exports
        self.main.LOG_PATH = self.old_log
        db_writer.flush()
        gc.collect()
        self.tmp.cleanup()

    def add_account(self, email, *, days=8, graph="ok", banned=False):
        ts = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
        with get_conn(self.db) as conn:
            cursor = conn.execute(
                """INSERT INTO accounts
                (email,password,client_id,refresh_token,status,health_status,health_severity,
                 graph_status,registered_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    email, "p", "cid", "rt", "banned" if banned else "all",
                    "banned" if banned else "all", "banned" if banned else "ok",
                    "" if banned else graph, ts, ts, ts,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)


class TestJobHardening(unittest.TestCase):
    def test_custom_job_runs_registered_cleanup(self):
        cleaned = threading.Event()

        def runner(progress, _is_cancelled):
            progress.add_cleanup(cleaned.set)

        job_id = jobs.submit_custom("cleanup", 0, runner)
        deadline = time.time() + 3
        while jobs.get_job(job_id)["state"] == "running" and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(cleaned.wait(1))

    def test_job_views_are_deep_snapshots(self):
        job_id = jobs.create_job("snapshot", 1)
        jobs.update_job(job_id, reasons={"ok": {"saved": 1}}, items=[{"status": "ok"}])
        view = jobs.get_job(job_id)
        view["reasons"]["ok"]["saved"] = 99
        view["items"].append({"status": "fail"})
        fresh = jobs.get_job(job_id)
        self.assertEqual(fresh["reasons"]["ok"]["saved"], 1)
        self.assertEqual(len(fresh["items"]), 1)

    def test_global_worker_budget_spans_jobs(self):
        active = 0
        peak = 0
        guard = threading.Lock()

        def worker(item):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.04)
            with guard:
                active -= 1
            return {"status": "ok", "account_id": item}

        with mock.patch.object(jobs, "_worker_slots", threading.BoundedSemaphore(2)):
            job_ids = [
                jobs.submit("limited", list(range(6)), worker, max_workers=4),
                jobs.submit("limited", list(range(6, 12)), worker, max_workers=4),
            ]
            deadline = time.time() + 5
            while time.time() < deadline:
                if all(jobs.get_job(job_id)["state"] != "running" for job_id in job_ids):
                    break
                time.sleep(0.02)
        self.assertLessEqual(peak, 2)
        self.assertEqual([jobs.get_job(job_id)["succeeded"] for job_id in job_ids], [6, 6])

    def test_worker_internal_type_error_runs_once(self):
        calls = 0

        def worker(_item, task=None):
            nonlocal calls
            calls += 1
            raise TypeError("inside")

        job_id = jobs.submit("type-error", [1], worker)
        deadline = time.time() + 3
        while jobs.get_job(job_id)["state"] == "running" and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(calls, 1)
        self.assertEqual(jobs.get_job(job_id)["failed"], 1)

    def test_running_persisted_job_is_marked_interrupted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "jobs.db"
            init_db(db)
            job_store.save(db, {"id": "j1", "type": "refresh", "state": "running", "started_at": "a"})
            job_store.mark_interrupted(db, "finished")
            restored = job_store.load(db)[0]
            self.assertEqual(restored["state"], "failed")
            self.assertEqual(restored["finished_at"], "finished")

    def test_db_writer_connection_error_wakes_waiter(self):
        with mock.patch.object(db_writer, "get_conn", side_effect=RuntimeError("open failed")):
            with self.assertRaisesRegex(RuntimeError, "open failed"):
                db_writer.execute(Path("unused.db"), lambda conn: None)

    def test_runtime_lease_allows_single_owner(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "lease.db"
            init_db(db)
            first = RuntimeLease(db, ttl=2)
            second = RuntimeLease(db, ttl=2)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()


class TestCancellation(TempMainTest):
    def test_refresh_cancelled_after_request_does_not_write(self):
        account_id = self.add_account("refresh@outlook.com")
        cancelled = threading.Event()
        task = mock.Mock()
        task.is_cancelled.side_effect = cancelled.is_set

        def response(*_args):
            cancelled.set()
            return {"success": True, "refresh_token": "new", "scope": "", "attempts": []}

        with mock.patch.object(self.main, "refresh_with_graph", side_effect=response):
            result = self.main.refresh_one(account_id, task=task)
        with get_conn(self.db) as conn:
            row = conn.execute("SELECT refresh_token,last_refresh_at FROM accounts WHERE id=?", (account_id,)).fetchone()
        self.assertEqual(result["status"], "skip")
        self.assertEqual((row["refresh_token"], row["last_refresh_at"]), ("rt", ""))

    def test_refresh_cancelled_while_waiting_for_writer_does_not_write(self):
        account_id = self.add_account("queued-refresh@outlook.com")
        cancelled = threading.Event()
        writer_entered = threading.Event()
        release_writer = threading.Event()
        task = mock.Mock()
        task.is_cancelled.side_effect = cancelled.is_set

        def block_writer(conn):
            writer_entered.set()
            release_writer.wait(2)

        blocker = threading.Thread(target=lambda: db_writer.execute(self.db, block_writer))
        blocker.start()
        self.assertTrue(writer_entered.wait(1))
        result = {}

        def refresh():
            result.update(self.main.refresh_one(account_id, task=task))

        response = {"success": True, "refresh_token": "new", "scope": "", "attempts": []}
        with mock.patch.object(self.main, "refresh_with_graph", return_value=response):
            worker = threading.Thread(target=refresh)
            worker.start()
            time.sleep(0.05)
            cancelled.set()
            release_writer.set()
            worker.join(2)
        blocker.join(2)
        with get_conn(self.db) as conn:
            row = conn.execute("SELECT refresh_token,last_refresh_at FROM accounts WHERE id=?", (account_id,)).fetchone()
            history_count = conn.execute("SELECT COUNT(*) FROM history WHERE account_id=?", (account_id,)).fetchone()[0]
        self.assertEqual(result["status"], "skip")
        self.assertEqual((row["refresh_token"], row["last_refresh_at"], history_count), ("rt", "", 0))

    def test_recovery_cancelled_after_browser_does_not_write(self):
        account_id = self.add_account("abuse@outlook.com", banned=True)
        cancelled = threading.Event()
        task = mock.Mock()
        task.is_cancelled.side_effect = cancelled.is_set

        def recovered(*_args, **_kwargs):
            cancelled.set()
            return {"success": True, "refresh_token": "new", "reason": "ok"}

        with mock.patch.object(self.main, "recover_abuse_account", side_effect=recovered):
            result = self.main.recover_abuse_one(account_id, task=task)
        with get_conn(self.db) as conn:
            row = conn.execute("SELECT refresh_token,health_status FROM accounts WHERE id=?", (account_id,)).fetchone()
        self.assertEqual(result["status"], "skip")
        self.assertEqual((row["refresh_token"], row["health_status"]), ("rt", "banned"))

    def test_batch_delete_cancelled_after_remote_keeps_local(self):
        account_id = self.add_account("delete@outlook.com")
        entered = threading.Event()
        release = threading.Event()

        def remote_delete(*_args):
            entered.set()
            release.wait(2)
            return {"success": True, "http_status": 200}

        bundle = (object(), "base", "csrf", False, {})
        with mock.patch.object(self.main, "_remote_session", return_value=bundle), mock.patch.object(
            self.main, "delete_account_remote", side_effect=remote_delete
        ):
            result = self.main.batch_delete(self.main.DeletePayload(ids=[account_id], remote=True, concurrency=1))
            self.assertTrue(entered.wait(2))
            jobs.cancel(result["job_id"])
            release.set()
            time.sleep(0.2)
        with get_conn(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounts WHERE id=?", (account_id,)).fetchone()[0], 1)


class TestAccountWriteQueue(TempMainTest):
    def test_concurrent_refresh_results_are_atomic(self):
        account_ids = [self.add_account(f"refresh-{index}@outlook.com") for index in range(32)]
        response = {"success": True, "refresh_token": "rotated", "scope": "", "attempts": []}
        with mock.patch.object(self.main, "refresh_with_graph", return_value=response), mock.patch.object(
            self.main, "auto_remote_sync"
        ), mock.patch.object(self.main, "schedule_stats_refresh"):
            with ThreadPoolExecutor(max_workers=16) as pool:
                results = list(pool.map(self.main.refresh_one, account_ids))

        self.assertTrue(all(result["status"] == "ok" for result in results))
        with get_conn(self.db) as conn:
            updated = conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE refresh_token='rotated' AND last_refresh_status='ok'"
            ).fetchone()[0]
            histories = conn.execute("SELECT COUNT(*) FROM history WHERE action='refresh' AND status='ok'").fetchone()[0]
        self.assertEqual((updated, histories), (len(account_ids), len(account_ids)))

    def test_concurrent_protocol_results_are_atomic(self):
        account_ids = [self.add_account(f"protocol-{index}@outlook.com") for index in range(32)]
        outcome = {
            "success": True,
            "health": {
                "health_status": "all",
                "health_severity": "ok",
                "ban_reason": "",
                "error_detail": "",
                "graph_status": "ok",
                "imap_status": "ok",
                "pop_status": "ok",
                "smtp_status": "ok",
            },
            "result": {},
        }
        with mock.patch.object(self.main, "run_protocol_test", return_value=outcome), mock.patch.object(
            self.main, "schedule_stats_refresh"
        ):
            with ThreadPoolExecutor(max_workers=16) as pool:
                results = list(pool.map(self.main.protocol_one, account_ids))

        self.assertTrue(all(result["status"] == "ok" for result in results))
        with get_conn(self.db) as conn:
            updated = conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE last_protocol_test_at<>'' AND graph_status='ok'"
            ).fetchone()[0]
            histories = conn.execute("SELECT COUNT(*) FROM history WHERE action='protocol' AND status='ok'").fetchone()[0]
        self.assertEqual((updated, histories), (len(account_ids), len(account_ids)))


class TestPagingAndExport(TempMainTest):
    def test_only_one_unresolved_export_is_created(self):
        account_id = self.add_account("atomic@outlook.com")
        ts = self.main.now_local()
        job = {
            "state": "running", "phase": "queued", "requested_count": 1,
            "domain": "outlook.com", "min_registered_days": 7, "retest": True,
            "concurrency": 1, "total": 1, "filename": "atomic.txt",
            "started_at": ts, "updated_at": ts,
        }
        items = [{"account_id": account_id, "email": "atomic@outlook.com", "domain": "outlook.com", "ordinal": 0}]
        export_store.create_job(self.db, {"id": "first", **job}, items, require_no_unresolved=True)
        with self.assertRaises(export_store.UnresolvedExportError) as raised:
            export_store.create_job(self.db, {"id": "second", **job}, items, require_no_unresolved=True)
        self.assertEqual(raised.exception.job_id, "first")

    def test_export_phase_claim_is_atomic(self):
        account_id = self.add_account("claim@outlook.com")
        ts = self.main.now_local()
        export_store.create_job(
            self.db,
            {"id": "claim", "state": "cancelled", "phase": "partial_ready", "requested_count": 1,
             "domain": "outlook.com", "min_registered_days": 7, "retest": True,
             "concurrency": 1, "total": 1, "filename": "claim.txt", "started_at": ts, "updated_at": ts},
            [{"account_id": account_id, "email": "claim@outlook.com", "domain": "outlook.com", "ordinal": 0}],
        )
        self.assertTrue(export_store.claim_job_phase(
            self.db, "claim", ("partial_ready",), state="running", phase="building", updated_at=ts
        ))
        self.assertFalse(export_store.claim_job_phase(
            self.db, "claim", ("partial_ready",), state="running", phase="building", updated_at=ts
        ))

    def test_server_paging_filter_and_sort(self):
        self.add_account("z@outlook.com")
        self.add_account("a@outlook.com")
        self.add_account("b@hotmail.com", banned=True)
        first = self.main.list_accounts(page=1, page_size=1, domain="outlook.com", sort="email", dir="asc")
        second = self.main.list_accounts(page=2, page_size=1, domain="outlook.com", sort="email", dir="asc")
        banned = self.main.list_accounts(filter="banned", page_size=10)
        self.assertEqual((first["total"], first["pages"]), (2, 2))
        self.assertEqual([first["items"][0]["email"], second["items"][0]["email"]], ["a@outlook.com", "z@outlook.com"])
        self.assertEqual(banned["items"][0]["email"], "b@hotmail.com")

    def test_export_retest_replenishes_failed_candidate(self):
        ids = [self.add_account(f"r{i}@outlook.com", days=8 + i) for i in range(3)]
        job_id = "replenish"
        ts = self.main.now_local()
        rows = []
        with get_conn(self.db) as conn:
            for account_id in ids:
                rows.append(dict(conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()))
        rows.sort(key=lambda row: row["registered_at"], reverse=True)
        export_store.create_job(
            self.db,
            {"id": job_id, "state": "running", "phase": "queued", "requested_count": 2,
             "domain": "outlook.com", "min_registered_days": 7, "retest": True,
             "concurrency": 2, "total": 3, "filename": "x.txt", "started_at": ts, "updated_at": ts},
            [{"account_id": row["id"], "email": row["email"], "domain": "outlook.com", "ordinal": i}
             for i, row in enumerate(rows)],
        )
        failed_id = rows[0]["id"]

        def protocol(account_id, job_id=None):
            if account_id == failed_id:
                with get_conn(self.db) as conn:
                    conn.execute("UPDATE accounts SET graph_status='',health_status='other_error' WHERE id=?", (account_id,))
                    conn.commit()
                return {"status": "fail", "reason": "bad"}
            return {"status": "ok"}

        with mock.patch.object(self.main, "protocol_one", side_effect=protocol):
            self.assertTrue(self.main._run_export_tests(job_id, lambda: False))
        counts = export_store.counts(self.db, job_id)
        self.assertEqual(counts["tested_ok"], 2)
        self.assertEqual(counts["test_failed"], 1)

    def test_export_rows_are_split_evenly_without_reordering(self):
        rows = [{"id": index} for index in range(10)]
        chunks = self.main.split_export_rows(rows, 3)
        self.assertEqual([len(chunk) for chunk in chunks], [4, 3, 3])
        self.assertEqual([row["id"] for chunk in chunks for row in chunk], list(range(10)))
        self.assertEqual([len(chunk) for chunk in self.main.split_export_rows(rows[:2], 10)], [1, 1])
        thousand = self.main.split_export_rows([{"id": index} for index in range(1000)], 10)
        self.assertEqual([len(chunk) for chunk in thousand], [100] * 10)

    def test_multi_file_export_creates_verified_zip_and_deletes_accounts(self):
        ids = [self.add_account(f"multi{i}@outlook.com") for i in range(5)]
        job_id = "multi-file"
        ts = self.main.now_local()
        export_store.create_job(
            self.db,
            {"id": job_id, "state": "running", "phase": "building", "requested_count": 5,
             "domain": "outlook.com", "min_registered_days": 7, "retest": False,
             "concurrency": 2, "total": 5, "filename": "multi.zip", "file_parts": 2,
             "started_at": ts, "updated_at": ts},
            [{"account_id": account_id, "email": f"multi{i}@outlook.com",
              "domain": "outlook.com", "ordinal": i, "test_state": "tested_ok"}
             for i, account_id in enumerate(ids)],
        )
        remote = (object(), "base", "csrf", False, {})
        with mock.patch.object(self.main, "_remote_session", return_value=remote), mock.patch.object(
            self.main, "delete_account_remote", return_value={"success": True, "http_status": 200}
        ):
            self.main._finalize_export(job_id, lambda: False)

        job = export_store.get_job(self.db, job_id)
        path = Path(job["file_path"])
        self.assertEqual((job["state"], job["phase"], job["file_parts"]), ("done", "done", 2))
        self.assertEqual(path.suffix, ".zip")
        self.assertEqual(self.main._export_checksum(path), job["checksum"])
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            lines = [archive.read(name).decode("utf-8").splitlines() for name in names]
        self.assertEqual(names, ["multi-001-of-002.txt", "multi-002-of-002.txt"])
        self.assertEqual([len(part) for part in lines], [3, 2])
        self.assertEqual(len({line.split("----", 1)[0] for part in lines for line in part}), 5)
        response = self.main.export_download(job_id)
        self.assertEqual(response.media_type, "application/zip")
        self.assertEqual(response.headers["x-file-parts"], "2")
        with get_conn(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
        self.assertTrue(self.main.delete_export_file(job_id)["success"])
        self.assertFalse(path.exists())

    def test_export_rejects_more_files_than_accounts(self):
        payload = self.main.ExportPayload(count=2, file_parts=3)
        with self.assertRaisesRegex(Exception, "文件个数不能超过导出数量"):
            self.main.export_preview(payload)

    def test_export_job_view_reports_target_tested_remaining_and_reserve(self):
        ids = [self.add_account(f"progress{i}@outlook.com") for i in range(6)]
        job_id = "export-progress"
        ts = self.main.now_local()
        export_store.create_job(
            self.db,
            {"id": job_id, "state": "running", "phase": "retesting", "requested_count": 3,
             "domain": "outlook.com", "min_registered_days": 7, "retest": True,
             "concurrency": 2, "total": 6, "filename": "progress.txt", "started_at": ts, "updated_at": ts},
            [{"account_id": account_id, "email": f"progress{i}@outlook.com",
              "domain": "outlook.com", "ordinal": i} for i, account_id in enumerate(ids)],
        )
        for account_id, state in zip(ids[:5], ["tested_ok", "tested_ok", "test_failed", "abused", "not_alive"]):
            export_store.update_item(self.db, job_id, account_id, test_state=state, updated_at=ts)

        view = export_store.job_view(self.db, export_store.get_job(self.db, job_id))

        self.assertEqual(view["export_requested_count"], 3)
        self.assertEqual(view["export_tested_count"], 5)
        self.assertEqual(view["export_remaining"], 1)
        self.assertEqual(view["export_reserve_pending"], 1)

    def test_checksum_expiry_and_manual_file_delete(self):
        account_id = self.add_account("file@outlook.com")
        job_id = "file-life"
        ts = self.main.now_local()
        export_store.create_job(
            self.db,
            {"id": job_id, "state": "running", "phase": "building", "requested_count": 1,
             "domain": "outlook.com", "min_registered_days": 7, "retest": False,
             "concurrency": 1, "total": 1, "filename": "file.txt", "started_at": ts, "updated_at": ts},
            [{"account_id": account_id, "email": "file@outlook.com", "domain": "outlook.com",
              "ordinal": 0, "test_state": "tested_ok"}],
        )
        with mock.patch.object(self.main, "_remote_session", return_value=(object(), "base", "csrf", False, {})), mock.patch.object(
            self.main, "delete_account_remote", return_value={"success": True, "http_status": 200}
        ):
            self.main._finalize_export(job_id, lambda: False)
        job = export_store.get_job(self.db, job_id)
        self.assertEqual(len(job["checksum"]), 64)
        self.assertEqual((Path(job["file_path"]).suffix, job["file_parts"]), (".txt", 1))
        self.assertTrue(Path(job["file_path"]).is_file())
        response = self.main.export_download(job_id)
        self.assertEqual(response.media_type, "text/plain; charset=utf-8")
        self.assertEqual(response.headers["x-file-parts"], "1")
        self.assertTrue(export_store.get_job(self.db, job_id)["downloaded_at"])
        Path(job["file_path"]).write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(Exception, "校验失败"):
            self.main.export_download(job_id)
        self.assertTrue(self.main.delete_export_file(job_id)["success"])
        self.assertFalse(Path(job["file_path"]).exists())

        expired_path = self.main.EXPORT_DIR / "expired.txt"
        expired_path.parent.mkdir(parents=True, exist_ok=True)
        expired_path.write_text("x", encoding="utf-8")
        export_store.update_job(
            self.db, job_id, file_path=str(expired_path), deleted_at="",
            expires_at=(datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds"),
        )
        self.assertEqual(self.main.cleanup_expired_exports(), 1)
        self.assertFalse(expired_path.exists())

    def test_retry_failed_remote_delete_updates_export_item(self):
        account_id = self.add_account("retry@outlook.com")
        ts = self.main.now_local()
        export_store.create_job(
            self.db,
            {"id": "retry-delete", "state": "done", "phase": "done", "requested_count": 1,
             "domain": "outlook.com", "min_registered_days": 7, "retest": False,
             "concurrency": 2, "total": 1, "filename": "retry.txt", "started_at": ts, "updated_at": ts},
            [{"account_id": account_id, "email": "retry@outlook.com", "domain": "outlook.com",
              "ordinal": 0, "test_state": "tested_ok", "remote_status": "failed:timeout"}],
        )
        bundle = (object(), "base", "csrf", False, {})
        with mock.patch.object(self.main, "_remote_session", return_value=bundle), mock.patch.object(
            self.main, "delete_account_remote", return_value={"success": True, "http_status": 204}
        ):
            result = self.main.retry_export_remote_delete("retry-delete")
            deadline = time.time() + 3
            while jobs.get_job(result["job_id"])["state"] == "running" and time.time() < deadline:
                time.sleep(0.02)
        item = export_store.get_items(self.db, "retry-delete")[0]
        self.assertEqual(item["remote_status"], "deleted")


class TestRemoteAndProtocol(unittest.TestCase):
    def test_thread_safe_session_rejects_requests_after_close(self):
        raw = mock.Mock()
        session = remote_pool.ThreadSafeSession(raw)
        session.close()
        session.close()
        raw.close.assert_called_once_with()
        with self.assertRaisesRegex(RuntimeError, "已关闭"):
            session.get("http://remote")
        raw.request.assert_not_called()

    def test_authenticated_pool_uses_independent_parallel_sessions(self):
        template = requests.Session()
        template.cookies.set("auth", "yes")
        pool = remote_pool.AuthenticatedSessionPool(template)
        sessions = []
        active = 0
        peak = 0
        lock = threading.Lock()

        def worker():
            nonlocal active, peak
            session = pool.session()
            sessions.append(session)
            self.assertEqual(session.cookies.get("auth"), "yes")
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.04)
            with lock:
                active -= 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len({id(session) for session in sessions}), 4)
        self.assertGreater(peak, 1)
        pool.close()
        pool.close()
        with self.assertRaisesRegex(RuntimeError, "已关闭"):
            pool.session()

    def test_remote_list_reads_all_pages(self):
        class Response:
            def __init__(self, payload): self.payload = payload
            def raise_for_status(self): return None
            def json(self): return self.payload

        class Session:
            def __init__(self): self.offsets = []
            def get(self, _url, params, **_kwargs):
                self.offsets.append(params["offset"])
                offset = params["offset"]
                page = [{"id": i} for i in range(offset, min(offset + 2, 5))]
                return Response({"items": page, "total": 5})

        session = Session()
        items = remote_pool.list_accounts(session, "http://remote", limit=2)
        self.assertEqual([item["id"] for item in items], [0, 1, 2, 3, 4])
        self.assertEqual(session.offsets, [0, 2, 4])

    def test_protocol_subprocess_receives_account_via_stdin(self):
        account = "a@x.com----p----cid----secret"
        completed = subprocess.run(
            [sys.executable, str(ROOT / "test_protocols.py"), "--stdin-account", "--skip-send"],
            input="bad-line\n",
            text=True,
            capture_output=True,
            timeout=10,
        )
        payload = json.loads(completed.stdout)
        self.assertIn("fatal_error", payload)
        self.assertNotIn(account, " ".join(completed.args))


class TestRemoteUpsertLocking(TempMainTest):
    def _row(self, account_id):
        with get_conn(self.db) as conn:
            return conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def test_remote_import_opens_db_only_after_remote_call(self):
        aid = self.add_account("lock-check@outlook.com")
        events = []
        real_get_conn = self.main.get_conn

        def fake_get_conn(path):
            events.append("db-open")
            return real_get_conn(path)

        with mock.patch.object(self.main, "find_account", return_value=None), mock.patch.object(
            self.main, "import_accounts",
            side_effect=lambda *a, **k: events.append("remote") or {"added_count": 1},
        ), mock.patch.object(self.main, "get_conn", side_effect=fake_get_conn):
            self.main._remote_upsert_one(
                mock.Mock(), "http://base", "csrf", False,
                {"provider": "outlook", "account_format": "client_id_refresh_token"},
                self._row(aid), lambda **kw: events.append(("progress", kw["status"])), "remote-import",
            )
        self.assertEqual(events, ["remote", "db-open", ("progress", "ok")])
        with get_conn(self.db) as conn:
            status = conn.execute("SELECT remote_sync_status FROM accounts WHERE id=?", (aid,)).fetchone()[0]
            history = conn.execute(
                "SELECT COUNT(*) FROM history WHERE account_id=? AND action='remote-import'", (aid,)
            ).fetchone()[0]
        self.assertEqual(status, "synced")
        self.assertEqual(history, 1)

    def test_retry_on_locked_retries_busy_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        self.assertEqual(self.main.retry_on_locked(flaky, retries=3, base_delay=0.01), "ok")
        self.assertEqual(calls["n"], 3)

    def test_retry_on_locked_rethrows_non_lock_error(self):
        def boom():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            self.main.retry_on_locked(boom, retries=3, base_delay=0.01)

    def test_remote_upsert_safe_records_fail_without_aborting_on_locked(self):
        aid = self.add_account("safe-lock@outlook.com")
        with mock.patch.object(
            self.main, "_remote_upsert_one", side_effect=sqlite3.OperationalError("database is locked")
        ):
            self.main._remote_upsert_safe(
                mock.Mock(), "http://base", "csrf", False, {}, self._row(aid), lambda **k: None, "remote-import",
            )
        with get_conn(self.db) as conn:
            status = conn.execute("SELECT remote_sync_status FROM accounts WHERE id=?", (aid,)).fetchone()[0]
        self.assertEqual(status, "fail")

    def test_remote_upsert_safe_logs_when_fail_record_write_locked(self):
        aid = self.add_account("safe-lock2@outlook.com")

        def no_retry(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with mock.patch.object(
            self.main, "_remote_upsert_one", side_effect=sqlite3.OperationalError("database is locked")
        ), mock.patch.object(self.main, "retry_on_locked", side_effect=no_retry), mock.patch.object(
            self.main, "db_writer"
        ) as fake_writer, mock.patch.object(self.main, "log_event") as log:
            fake_writer.execute.side_effect = sqlite3.OperationalError("database is locked")
            self.main._remote_upsert_safe(
                mock.Mock(), "http://base", "csrf", False, {}, self._row(aid), lambda **k: None, "remote-import",
            )
        self.assertTrue(log.called)


if __name__ == "__main__":
    unittest.main()
