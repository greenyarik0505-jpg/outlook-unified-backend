import { getAccessToken, graphRequest, extractOtp, setCors } from "./_helpers.js";

export default async function handler(req, res) {
  setCors(res);
  if (req.method === "OPTIONS") {
    return res.status(200).end();
  }

  try {
    const params = req.method === "POST" ? req.body : req.query;
    const clientId = params.client_id || process.env.CLIENT_ID;
    const refreshToken = params.refresh_token;
    const messageId = params.id;

    if (!refreshToken) {
      return res.status(400).json({ success: false, error: "Missing 'refresh_token'" });
    }
    if (!messageId) {
      return res.status(400).json({ success: false, error: "Missing message 'id'" });
    }

    const { accessToken, rotatedRefreshToken } = await getAccessToken(clientId, refreshToken);
    const item = await graphRequest(accessToken, `/me/messages/${encodeURIComponent(messageId)}`);

    const senderObj = item.from?.emailAddress || {};
    const bodyObj = item.body || {};
    const contentType = bodyObj.contentType || "Text";
    const content = bodyObj.content || "";
    const subject = item.subject || "";

    const otp = extractOtp(
      subject,
      contentType === "Text" ? content : item.bodyPreview || "",
      contentType === "html" ? content : ""
    );

    return res.status(200).json({
      success: true,
      id: item.id,
      subject,
      from_name: senderObj.name || "",
      from_email: senderObj.address || "",
      to: (item.toRecipients || []).map((r) => r.emailAddress?.address || ""),
      received_at: item.receivedDateTime,
      is_read: item.isRead || false,
      has_attachments: item.hasAttachments || false,
      body_type: contentType,
      body_content: content,
      body_preview: item.bodyPreview || "",
      otp,
      rotated_refresh_token: rotatedRefreshToken || null,
    });
  } catch (err) {
    return res.status(500).json({ success: false, error: err.message || "Failed to fetch message" });
  }
}
