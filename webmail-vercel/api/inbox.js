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
    const folder = params.folder || "inbox";
    const top = Math.min(Math.max(parseInt(params.top || "25", 10), 1), 50);
    const skip = parseInt(params.skip || "0", 10);
    const search = params.search || "";

    if (!refreshToken) {
      return res.status(400).json({ success: false, error: "Missing 'refresh_token' parameter" });
    }

    const { accessToken, rotatedRefreshToken } = await getAccessToken(clientId, refreshToken);

    const queryParams = {
      $top: top,
      $skip: skip,
      $select: "id,subject,from,toRecipients,receivedDateTime,hasAttachments,isRead,bodyPreview,importance",
      $orderby: "receivedDateTime desc",
    };
    if (search) {
      queryParams.$search = `"${search}"`;
    }

    const endpoint = folder.toLowerCase() === "all" ? "/me/messages" : `/me/mailFolders/${folder}/messages`;
    const graphData = await graphRequest(accessToken, endpoint, queryParams);

    const messages = (graphData.value || []).map((item) => {
      const senderObj = item.from?.emailAddress || {};
      const subject = item.subject || "(Без темы)";
      const preview = item.bodyPreview || "";
      const otp = extractOtp(subject, preview);

      return {
        id: item.id,
        subject,
        from_name: senderObj.name || "",
        from_email: senderObj.address || "",
        received_at: item.receivedDateTime,
        is_read: item.isRead || false,
        has_attachments: item.hasAttachments || false,
        preview,
        quick_code: otp.code,
        quick_link: otp.link,
      };
    });

    return res.status(200).json({
      success: true,
      count: messages.length,
      messages,
      rotated_refresh_token: rotatedRefreshToken || null,
    });
  } catch (err) {
    return res.status(500).json({ success: false, error: err.message || "Internal server error" });
  }
}
