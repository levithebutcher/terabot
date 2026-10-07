// Cloudflare Worker Script for TeraBox Reverse Proxy
// Free tier: 100,000 requests/day

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Handle CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, HEAD, POST, OPTIONS",
          "Access-Control-Allow-Headers": "*",
        },
      });
    }

    // Health check endpoint
    if (url.pathname === "/health") {
      return new Response(JSON.stringify({ status: "ok", service: "terabox-proxy" }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // Expected parameter: ?target_url=https://www.terabox.app/...
    const targetUrlStr = url.searchParams.get("target_url");
    if (!targetUrlStr) {
      return new Response(
        JSON.stringify({ error: "Missing 'target_url' query parameter" }),
        { status: 400, headers: { "Content-Type": "application/json" } }
      );
    }

    let targetUrl;
    try {
      targetUrl = new URL(targetUrlStr);
    } catch (e) {
      return new Response(
        JSON.stringify({ error: "Invalid 'target_url'" }),
        { status: 400, headers: { "Content-Type": "application/json" } }
      );
    }

    // Forward headers from incoming request, override Host & Referer
    const forwardHeaders = new Headers(request.headers);
    forwardHeaders.set("Host", targetUrl.host);
    forwardHeaders.set("Referer", `https://${targetUrl.host}/`);
    forwardHeaders.set(
      "User-Agent",
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    );

    try {
      const response = await fetch(targetUrl.toString(), {
        method: request.method,
        headers: forwardHeaders,
        body: request.method !== "GET" && request.method !== "HEAD" ? request.body : null,
        redirect: "follow",
      });

      const respHeaders = new Headers(response.headers);
      respHeaders.set("Access-Control-Allow-Origin", "*");

      return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers: respHeaders,
      });
    } catch (err) {
      return new Response(
        JSON.stringify({ error: "Upstream fetch error", details: err.message }),
        { status: 502, headers: { "Content-Type": "application/json" } }
      );
    }
  },
};
