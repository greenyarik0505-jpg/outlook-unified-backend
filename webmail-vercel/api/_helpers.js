// Shared helpers for Vercel Serverless Functions
const TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token";
const GRAPH_BASE = "https://graph.microsoft.com/v1.0";
const DEFAULT_CLIENT_ID = "d3590ed6-52b3-4102-aeff-aad2292ab01c";

export function setCors(res) {
  res.setHeader("Access-Control-Allow-Credentials", "true");
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET,OPTIONS,PATCH,DELETE,POST,PUT");
  res.setHeader(
    "Access-Control-Allow-Headers",
    "X-CSRF-Token, X-Requested-With, Accept, Accept-Version, Content-Length, Content-MD5, Content-Type, Date, X-Api-Version, Authorization"
  );
}

export async function getAccessToken(clientId, refreshToken) {
  const cId = clientId || DEFAULT_CLIENT_ID;
  const body = new URLSearchParams({
    client_id: cId,
    grant_type: "refresh_token",
    refresh_token: refreshToken,
    scope: "https://graph.microsoft.com/.default",
  });

  const resp = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });

  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.error_description || data.error || "Failed to exchange refresh token");
  }

  return {
    accessToken: data.access_token,
    rotatedRefreshToken: data.refresh_token,
    expiresIn: data.expires_in,
  };
}

export function extractOtp(subject = "", bodyText = "", bodyHtml = "") {
  let fullText = `${subject}\n${bodyText}`;
  if (bodyHtml) {
    const cleanHtml = bodyHtml
      .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, "")
      .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, "")
      .replace(/<[^>]+>/g, " ");
    fullText += `\n${cleanHtml}`;
  }

  let code = null;
  let link = null;

  // 1. Tagged digits in HTML (e.g. <b>123456</b>, <h2>89210</h2>)
  if (bodyHtml) {
    const htmlMatch = bodyHtml.match(/<(?:b|strong|h[1-4]|code|span)[^>]*?>\s*([0-9]{4,8})\s*<\/(?:b|strong|h[1-4]|code|span)>/i);
    if (htmlMatch) {
      code = htmlMatch[1].trim();
    }
  }

  // 2. English / Russian phrase patterns
  if (!code) {
    const patterns = [
      /(?:verification|security|confirm(?:ation)?|access|login|one-time|auth)?\s*(?:code|pin|otp|password|passcode)\s*(?:is|:|-|=|\u2013|\u2014|)\s*([A-Za-z0-9]{4,8})\b/i,
      /(?:проверочный|секретный|одноразовый|код|пароль)\s*(?:подтверждения|авторизации|безопасности|доступа)?\s*(?:это|:|-|=|\u2013|\u2014|)\s*([0-9]{4,8})\b/i,
      /(?:код|code)[\s:=#№–—]+([0-9]{4,8})\b/i,
      /\b([2-9BCDFGHJKMNPQRTVWXYZ]{5})\b/, // Steam Guard format
    ];
    for (const pat of patterns) {
      const m = fullText.match(pat);
      if (m) {
        code = m[1].trim();
        break;
      }
    }
  }

  // 3. Fallback standalone digits in subject
  if (!code) {
    const subMatch = subject.match(/\b([0-9]{4,8})\b/);
    if (subMatch) {
      code = subMatch[1].trim();
    }
  }

  // 4. Verification link
  const linkMatch = fullText.match(/https?:\/\/[^\s<>"']+(?:verify|confirm|activation|validate|auth|token=[a-zA-Z0-9_\-\.]+)[^\s<>"']*/i);
  if (linkMatch) {
    link = linkMatch[0].replace(/[.,;!)]+$/, "");
  }

  return { code, link };
}

export async function graphRequest(accessToken, endpoint, params = {}) {
  const url = new URL(`${GRAPH_BASE}${endpoint}`);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) {
      url.searchParams.set(k, String(v));
    }
  }

  const resp = await fetch(url.toString(), {
    headers: {
      Authorization: `Bearer ${accessToken}`,
      Accept: "application/json",
    },
  });

  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.error?.message || "Microsoft Graph API error");
  }
  return data;
}
