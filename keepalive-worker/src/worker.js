export default {
  async fetch(_request, env) {
    const result = await pingTarget(env);
    return Response.json(result, { status: result.ok ? 200 : 502 });
  },

  async scheduled(_event, env, _ctx) {
    const result = await pingTarget(env);
    console.log(JSON.stringify(result));
  },
};

async function pingTarget(env) {
  const targetUrl = env.TARGET_URL;

  if (!targetUrl || targetUrl.includes("your-render-app")) {
    return {
      ok: false,
      error: "Set TARGET_URL to your Render /health URL.",
    };
  }

  const started = Date.now();

  try {
    const response = await fetch(targetUrl, {
      method: "GET",
      headers: {
        "user-agent": "cloudflare-worker-render-keepalive/1.0",
      },
    });

    return {
      ok: response.ok,
      status: response.status,
      elapsed_ms: Date.now() - started,
      target_host: new URL(targetUrl).host,
      checked_at: new Date().toISOString(),
    };
  } catch (error) {
    return {
      ok: false,
      error: String(error?.message || error),
      elapsed_ms: Date.now() - started,
      target_host: safeHost(targetUrl),
      checked_at: new Date().toISOString(),
    };
  }
}

function safeHost(value) {
  try {
    return new URL(value).host;
  } catch {
    return "invalid-url";
  }
}
