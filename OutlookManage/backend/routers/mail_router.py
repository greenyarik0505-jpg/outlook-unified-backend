"""FastAPI router for Mail reader and Auto-registration bridge APIs.
Allows reading incoming emails, message inspection, and OTP extraction via Microsoft Graph.
"""

from __future__ import annotations

import time
from typing import Any, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.db import get_conn, retry_on_locked
from backend.services.mail_service import (
    delete_message,
    extract_otp,
    get_access_token_direct,
    get_message_detail,
    list_messages,
    mark_message_read,
    DEFAULT_CLIENT_ID,
)
from backend.services.autoreg_manager import autoreg_mgr

router = APIRouter(prefix="/api/mail", tags=["Mail"])
autoreg_router = APIRouter(prefix="/api/autoreg", tags=["AutoReg"])


def _get_account_from_db(email_or_id: str) -> dict[str, Any]:
    with get_conn() as conn:
        cursor = conn.cursor()
        if email_or_id.isdigit():
            cursor.execute("SELECT id, email, client_id, refresh_token, status, health_status FROM accounts WHERE id = ?", (int(email_or_id),))
        else:
            cursor.execute("SELECT id, email, client_id, refresh_token, status, health_status FROM accounts WHERE email = ?", (email_or_id.strip(),))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Account '{email_or_id}' not found in database")
        return dict(row)


def _resolve_access_token(account: dict[str, Any], proxy_url: Optional[str] = None) -> str:
    token_res = get_access_token_direct(
        client_id=account.get("client_id") or DEFAULT_CLIENT_ID,
        refresh_token=account.get("refresh_token") or "",
        proxy_url=proxy_url,
    )
    if not token_res.get("success"):
        raise HTTPException(
            status_code=400,
            detail=f"Failed to refresh access token: {token_res.get('error', 'unknown error')}",
        )

    # If Microsoft rotated the refresh_token, persist it to the database
    rotated = token_res.get("rotated_refresh_token")
    if rotated and rotated != account.get("refresh_token") and account.get("id"):
        try:
            with get_conn() as conn:
                cursor = conn.cursor()
                now_str = time.strftime("%Y-%m-%d %H:%M:%S")
                retry_on_locked(
                    cursor.execute,
                    "UPDATE accounts SET refresh_token = ?, refresh_token_updated_at = ?, updated_at = ? WHERE id = ?",
                    (rotated, now_str, now_str, account["id"]),
                )
                conn.commit()
        except Exception:
            pass

    return str(token_res.get("access_token", ""))


@router.get("/accounts")
def list_mail_accounts():
    """List all accounts available in database with basic status info."""
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, email, status, health_status, health_severity, 
                   registered_at, refresh_token_updated_at, created_at
            FROM accounts
            ORDER BY id DESC
            """
        )
        rows = [dict(r) for r in cursor.fetchall()]
        return {"success": True, "count": len(rows), "accounts": rows}


@router.get("/inbox")
def get_inbox_messages(
    email: str = Query(..., description="Target email address or account ID"),
    folder: str = Query("inbox", description="Folder: inbox, junkemail, sentitems, or all"),
    top: int = Query(25, ge=1, le=50, description="Max messages to return"),
    skip: int = Query(0, ge=0, description="Offset for pagination"),
    search: Optional[str] = Query(None, description="Optional search term"),
):
    """Retrieve message list for an account from Microsoft Graph."""
    account = _get_account_from_db(email)
    access_token = _resolve_access_token(account)

    res = list_messages(
        access_token=access_token,
        folder=folder,
        top=top,
        skip=skip,
        search=search,
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Error fetching messages"))
    return res


@router.get("/message/{message_id}")
def get_message_content(
    message_id: str,
    email: str = Query(..., description="Target email address or account ID"),
):
    """Retrieve full email content (HTML, text, headers, attachments, and extracted OTP)."""
    account = _get_account_from_db(email)
    access_token = _resolve_access_token(account)

    res = get_message_detail(access_token=access_token, message_id=message_id)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Error fetching message detail"))
    return res


@router.get("/otp")
def get_otp_code(
    email: str = Query(..., description="Target email address or account ID"),
    service: Optional[str] = Query(None, description="Filter by service name, e.g. 'discord', 'steam', 'google'"),
    timeout: int = Query(30, ge=0, le=120, description="Polling timeout in seconds"),
    max_age_seconds: int = Query(600, ge=30, le=86400, description="Maximum age of email in seconds"),
):
    """Wait and extract verification code / OTP / activation link from the latest matching email."""
    account = _get_account_from_db(email)
    deadline = time.time() + timeout

    while True:
        access_token = _resolve_access_token(account)
        res = list_messages(access_token=access_token, folder="inbox", top=10)

        if res.get("success") and res.get("messages"):
            messages = res["messages"]
            for msg in messages:
                # Check service filter
                if service:
                    s_lower = service.lower()
                    if s_lower not in msg.get("subject", "").lower() and s_lower not in msg.get("from_email", "").lower():
                        continue

                # Fetch full message detail for accurate OTP extraction
                detail = get_message_detail(access_token=access_token, message_id=msg["id"])
                if detail.get("success"):
                    otp_data = detail.get("otp") or {}
                    if otp_data.get("code") or otp_data.get("link"):
                        return {
                            "success": True,
                            "email": account["email"],
                            "code": otp_data.get("code"),
                            "link": otp_data.get("link"),
                            "rule": otp_data.get("rule"),
                            "subject": detail.get("subject"),
                            "from_email": detail.get("from_email"),
                            "received_at": detail.get("received_at"),
                            "message_id": detail.get("id"),
                        }

        if time.time() >= deadline:
            break
        time.sleep(3)

    return {
        "success": False,
        "error": "timeout",
        "message": f"No verification code found within {timeout}s",
    }


@router.get("/latest")
def get_latest_message(
    email: str = Query(..., description="Target email address or account ID"),
):
    """Quickly retrieve the newest email in the inbox."""
    account = _get_account_from_db(email)
    access_token = _resolve_access_token(account)
    res = list_messages(access_token=access_token, folder="inbox", top=1)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    messages = res.get("messages", [])
    if not messages:
        return {"success": True, "message": None}
    detail = get_message_detail(access_token=access_token, message_id=messages[0]["id"])
    return detail


class DirectMailRequest(BaseModel):
    client_id: Optional[str] = DEFAULT_CLIENT_ID
    refresh_token: str
    folder: Optional[str] = "inbox"
    top: Optional[int] = 25
    search: Optional[str] = None


class DirectOtpRequest(BaseModel):
    client_id: Optional[str] = DEFAULT_CLIENT_ID
    refresh_token: str
    service: Optional[str] = None
    timeout: Optional[int] = 10
    max_age_seconds: Optional[int] = 600


@router.post("/direct/inbox")
def direct_inbox_query(req: DirectMailRequest):
    """Stateless message reader for external bots/scripts providing refresh_token directly."""
    token_res = get_access_token_direct(
        client_id=req.client_id or DEFAULT_CLIENT_ID,
        refresh_token=req.refresh_token,
    )
    if not token_res.get("success"):
        raise HTTPException(status_code=400, detail=token_res.get("error"))

    res = list_messages(
        access_token=token_res["access_token"],
        folder=req.folder or "inbox",
        top=req.top or 25,
        search=req.search,
    )
    return res


@router.post("/direct/otp")
def direct_otp_query(req: DirectOtpRequest):
    """Stateless OTP extraction for external bots/scripts providing refresh_token directly."""
    token_res = get_access_token_direct(
        client_id=req.client_id or DEFAULT_CLIENT_ID,
        refresh_token=req.refresh_token,
    )
    if not token_res.get("success"):
        raise HTTPException(status_code=400, detail=token_res.get("error"))

    access_token = token_res["access_token"]
    res = list_messages(access_token=access_token, folder="inbox", top=5)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error"))

    for msg in res.get("messages", []):
        if req.service:
            s_lower = req.service.lower()
            if s_lower not in msg.get("subject", "").lower() and s_lower not in msg.get("from_email", "").lower():
                continue
        detail = get_message_detail(access_token=access_token, message_id=msg["id"])
        if detail.get("success"):
            otp_data = detail.get("otp") or {}
            if otp_data.get("code") or otp_data.get("link"):
                return {
                    "success": True,
                    "code": otp_data.get("code"),
                    "link": otp_data.get("link"),
                    "subject": detail.get("subject"),
                    "from_email": detail.get("from_email"),
                    "received_at": detail.get("received_at"),
                }

    return {"success": False, "error": "No OTP code found"}


class StartAutoregRequest(BaseModel):
    concurrent: Optional[int] = 1
    tasks: Optional[int] = 5
    email_suffix: Optional[str] = "@outlook.com"
    headless: Optional[bool] = True
    proxy: Optional[str] = None


# AutoReg router endpoints
@autoreg_router.post("/start")
def start_autoreg(req: Optional[StartAutoregRequest] = None):
    """Start the Outlook auto-registration background process."""
    r = req or StartAutoregRequest()
    return autoreg_mgr.start(
        concurrent=r.concurrent or 1,
        tasks=r.tasks or 5,
        email_suffix=r.email_suffix or "@outlook.com",
        headless=r.headless if r.headless is not None else True,
        proxy=r.proxy,
    )


@autoreg_router.post("/stop")
def stop_autoreg():
    """Stop the running auto-registration worker."""
    return autoreg_mgr.stop()


@autoreg_router.get("/status")
def status_autoreg():
    """Get the current auto-registration worker status."""
    return autoreg_mgr.status()


@autoreg_router.get("/logs")
def get_autoreg_logs(limit: int = 150):
    """Get live logs of the auto-registration process."""
    return {
        "success": True,
        "lines": autoreg_mgr.get_logs(limit=limit),
        "status": autoreg_mgr.status(),
    }


@autoreg_router.post("/sync")
def sync_autoreg():
    """Force scan Results/oauth2.txt and sync new accounts to database."""
    imported = autoreg_mgr.sync_results()
    return {"success": True, "imported_count": imported}
