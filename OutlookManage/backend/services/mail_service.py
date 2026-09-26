"""Mail Service: Microsoft Graph OAuth2 mail reader and OTP extractor.
Supports mailbox reading, message inspection, and automated verification code retrieval.
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests
import urllib3

urllib3.disable_warnings()

TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
DEFAULT_CLIENT_ID = "d3590ed6-52b3-4102-aeff-aad2292ab01c"  # Standard Microsoft Office client ID
HTTP_TIMEOUT = 25


def _build_session(proxy_url: Optional[str] = None) -> requests.Session:
    session = requests.Session()
    session.verify = False
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


def get_access_token_direct(
    client_id: str,
    refresh_token: str,
    proxy_url: Optional[str] = None,
    scope: str = GRAPH_SCOPE,
) -> dict[str, Any]:
    """Exchange refresh_token for a fresh access_token via Microsoft OAuth2."""
    session = _build_session(proxy_url)
    data = {
        "client_id": client_id or DEFAULT_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": scope,
    }
    try:
        resp = session.post(TOKEN_URL, data=data, timeout=HTTP_TIMEOUT)
    except Exception as exc:
        return {"success": False, "error": f"Network error during token exchange: {exc}"}

    if resp.status_code == 200:
        payload = resp.json()
        return {
            "success": True,
            "access_token": payload.get("access_token", ""),
            "rotated_refresh_token": payload.get("refresh_token", ""),
            "expires_in": payload.get("expires_in", 3600),
        }

    try:
        err_json = resp.json()
        err_msg = err_json.get("error_description") or err_json.get("error") or resp.text
    except Exception:
        err_msg = resp.text[:400]
    return {
        "success": False,
        "status_code": resp.status_code,
        "error": err_msg,
    }


def extract_otp(subject: str = "", text_body: str = "", html_body: str = "") -> dict[str, Any]:
    """Extract verification code / OTP and confirmation link from email contents."""
    full_text = f"{subject}\n{text_body}"
    clean_html_text = ""
    if html_body:
        # Strip script/style tags then tags
        cleaned = re.sub(r"(?is)<(script|style).*?>.*?(</\1>)", "", html_body)
        clean_html_text = re.sub(r"<[^>]+>", " ", cleaned)
        clean_html_text = html.unescape(clean_html_text)
        full_text += f"\n{clean_html_text}"

    extracted_code = None
    extracted_link = None
    rule_name = None

    # 1. Look for HTML emphasized codes (e.g. <b>123456</b>, <strong class="code">89210</strong>, etc.)
    if html_body:
        html_code_match = re.search(
            r"<(?:b|strong|h[1-4]|code|span)[^>]*?>\s*([0-9]{4,8})\s*</(?:b|strong|h[1-4]|code|span)>",
            html_body,
            re.IGNORECASE,
        )
        if html_code_match:
            extracted_code = html_code_match.group(1).strip()
            rule_name = "html_tag_digits"

    # 2. Key phrases in Russian / English (e.g. "code is 123456", "код: 123456", "verification code: 123456")
    if not extracted_code:
        patterns = [
            (
                r"(?i)(?:verification|security|confirm(?:ation)?|access|login|one-time|auth|authorisation|authorization)?\s*(?:code|pin|otp|password|passcode)\s*(?:is|:|-|=|\u2013|\u2014|)\s*([A-Za-z0-9]{4,8})\b",
                "phrase_code_en",
            ),
            (
                r"(?i)(?:проверочный|секретный|одноразовый|код|пароль)\s*(?:подтверждения|авторизации|безопасности|доступа)?\s*(?:это|:|-|=|\u2013|\u2014|)\s*([0-9]{4,8})\b",
                "phrase_code_ru",
            ),
            (
                r"(?i)(?:код|code)[\s:=#№–—]+([0-9]{4,8})\b",
                "short_code",
            ),
            (
                r"\b([2-9BCDFGHJKMNPQRTVWXYZ]{5})\b",  # Steam Guard style 5-char alphanumeric
                "steam_guard",
            ),
        ]
        for pat, name in patterns:
            m = re.search(pat, full_text)
            if m:
                extracted_code = m.group(1).strip()
                rule_name = name
                break

    # 3. Fallback: Standalone 4 to 8 consecutive digits in a line by itself or in subject
    if not extracted_code:
        m_subj = re.search(r"\b([0-9]{4,8})\b", subject)
        if m_subj:
            extracted_code = m_subj.group(1).strip()
            rule_name = "subject_digits"

    # 4. Confirmation / Verification link extraction
    link_match = re.search(
        r"https?://[^\s<>\"']+(?:verify|confirm|activation|validate|auth|token=[a-zA-Z0-9_\-\.]+)[^\s<>\"']*",
        full_text,
        re.IGNORECASE,
    )
    if link_match:
        extracted_link = link_match.group(0).rstrip(".,;!)]")

    return {
        "code": extracted_code,
        "link": extracted_link,
        "rule": rule_name,
    }


def list_messages(
    access_token: str,
    folder: str = "inbox",
    top: int = 25,
    skip: int = 0,
    search: Optional[str] = None,
    proxy_url: Optional[str] = None,
) -> dict[str, Any]:
    """Retrieve message list for the authenticated user via Microsoft Graph."""
    session = _build_session(proxy_url)
    headers = {"Authorization": f"Bearer {access_token}"}
    top = max(1, min(top, 50))

    if folder.lower() in ("all", "messages"):
        url = f"{GRAPH_BASE}/me/messages"
    else:
        url = f"{GRAPH_BASE}/me/mailFolders/{folder}/messages"

    params: dict[str, Any] = {
        "$top": top,
        "$skip": skip,
        "$select": "id,subject,from,toRecipients,receivedDateTime,hasAttachments,isRead,bodyPreview,importance",
        "$orderby": "receivedDateTime desc",
    }
    if search:
        params["$search"] = f'"{search}"'

    try:
        resp = session.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
    except Exception as exc:
        return {"success": False, "error": f"Failed to connect to Microsoft Graph: {exc}"}

    if resp.status_code != 200:
        return {
            "success": False,
            "status_code": resp.status_code,
            "error": resp.text[:400],
        }

    raw_items = resp.json().get("value", [])
    parsed_messages = []
    for item in raw_items:
        sender_obj = item.get("from") or {}
        email_addr_obj = sender_obj.get("emailAddress") or {}
        sender_name = email_addr_obj.get("name") or ""
        sender_email = email_addr_obj.get("address") or ""
        subject = item.get("subject") or "(No Subject)"
        preview = item.get("bodyPreview") or ""

        # Pre-check for OTP in preview & subject for quick UI badges
        otp_info = extract_otp(subject=subject, text_body=preview)

        parsed_messages.append({
            "id": item.get("id"),
            "subject": subject,
            "from_name": sender_name,
            "from_email": sender_email,
            "received_at": item.get("receivedDateTime"),
            "is_read": item.get("isRead", False),
            "has_attachments": item.get("hasAttachments", False),
            "preview": preview,
            "quick_code": otp_info.get("code"),
            "quick_link": otp_info.get("link"),
        })

    return {
        "success": True,
        "count": len(parsed_messages),
        "messages": parsed_messages,
    }


def get_message_detail(
    access_token: str,
    message_id: str,
    proxy_url: Optional[str] = None,
) -> dict[str, Any]:
    """Fetch complete email content including HTML/text body and attachments."""
    session = _build_session(proxy_url)
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{GRAPH_BASE}/me/messages/{message_id}"

    try:
        resp = session.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    except Exception as exc:
        return {"success": False, "error": f"Failed to get message: {exc}"}

    if resp.status_code != 200:
        return {"success": False, "status_code": resp.status_code, "error": resp.text[:400]}

    item = resp.json()
    sender_obj = item.get("from") or {}
    email_addr_obj = sender_obj.get("emailAddress") or {}

    body_obj = item.get("body") or {}
    content_type = body_obj.get("contentType", "Text")
    content = body_obj.get("content", "")
    subject = item.get("subject") or ""

    otp_info = extract_otp(
        subject=subject,
        text_body=content if content_type == "Text" else item.get("bodyPreview", ""),
        html_body=content if content_type == "html" else "",
    )

    return {
        "success": True,
        "id": item.get("id"),
        "subject": subject,
        "from_name": email_addr_obj.get("name", ""),
        "from_email": email_addr_obj.get("address", ""),
        "to": [
            rcpt.get("emailAddress", {}).get("address", "")
            for rcpt in (item.get("toRecipients") or [])
        ],
        "received_at": item.get("receivedDateTime"),
        "is_read": item.get("isRead", False),
        "has_attachments": item.get("hasAttachments", False),
        "body_type": content_type,
        "body_content": content,
        "body_preview": item.get("bodyPreview", ""),
        "otp": otp_info,
    }


def mark_message_read(
    access_token: str,
    message_id: str,
    is_read: bool = True,
    proxy_url: Optional[str] = None,
) -> bool:
    """Mark message as read or unread."""
    session = _build_session(proxy_url)
    url = f"{GRAPH_BASE}/me/messages/{message_id}"
    resp = session.patch(
        url,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"isRead": is_read},
        timeout=HTTP_TIMEOUT,
    )
    return resp.status_code in (200, 204)


def delete_message(
    access_token: str,
    message_id: str,
    proxy_url: Optional[str] = None,
) -> bool:
    """Delete a message."""
    session = _build_session(proxy_url)
    url = f"{GRAPH_BASE}/me/messages/{message_id}"
    resp = session.delete(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=HTTP_TIMEOUT,
    )
    return resp.status_code in (200, 204)
