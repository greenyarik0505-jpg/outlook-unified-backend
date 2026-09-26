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
    const service = (params.service || "").toLowerCase();

    if (!refreshToken) {
      return res.status(400).json({ success: false, error: "Missing 'refresh_token'" });
    }

    const { accessToken } = await getAccessToken(clientId, refreshToken);
    const graphData = await graphRequest(accessToken, "/me/mailFolders/inbox/messages", {
      $top: 10,
      $select: "id,subject,from,bodyPreview,body,receivedDateTime",
      $orderby: "receivedDateTime desc",
    });

    const messages = graphData.value || [];
    for (const msg of messages) {
      const subject = msg.subject || "";
      const sender = msg.from?.emailAddress?.address || "";

      if (service && !subject.toLowerCase().includes(service) && !sender.toLowerCase().includes(service)) {
        continue;
      }

      const bodyText = msg.body?.contentType === "Text" ? msg.body?.content : msg.bodyPreview || "";
      const bodyHtml = msg.body?.contentType === "html" ? msg.body?.content : "";
      const otp = extractOtp(subject, bodyText, bodyHtml);

      if (otp.code || otp.link) {
        return res.status(200).json({
          success: true,
          code: otp.code,
          link: otp.link,
          subject,
          from_email: sender,
          received_at: msg.receivedDateTime,
          message_id: msg.id,
        });
      }
    }

    return res.status(404).json({
      success: false,
      error: "No OTP code found in recent emails",
    });
  } catch (err) {
    return res.status(500).json({ success: false, error: err.message || "Failed to extract OTP" });
  }
}
