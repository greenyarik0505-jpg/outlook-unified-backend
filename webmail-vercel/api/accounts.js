import { setCors } from "./_helpers.js";

export default async function handler(req, res) {
  setCors(res);
  if (req.method === "OPTIONS") {
    return res.status(200).end();
  }

  const backendUrl = req.headers["x-backend-url"] || process.env.BACKEND_URL;
  if (!backendUrl) {
    return res.status(200).json({
      success: true,
      has_backend: false,
      message: "No BACKEND_URL configured. Use manual account connect or configure backend URL.",
      accounts: [],
    });
  }

  try {
    const cleanUrl = backendUrl.replace(/\/+$/, "");
    const targetUrl = `${cleanUrl}/api/mail/accounts`;
    const resp = await fetch(targetUrl, {
      headers: {
        Accept: "application/json",
      },
    });

    const data = await resp.json();
    return res.status(resp.status).json(data);
  } catch (err) {
    return res.status(502).json({
      success: false,
      error: `Failed to reach backend at ${backendUrl}: ${err.message}`,
    });
  }
}
