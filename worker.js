// Entry point for the Worker deploy (see wrangler.toml). Everything except
// the two dynamic routes below is a static asset served straight from the
// repo — this Worker exists to add /dl/:name and /event.
const SAFE_NAME = /^[a-f0-9]{6,32}\.(webp|jpg|jpeg|png)$/;
const EVENTS = new Set(["open", "filter", "preview"]);
const SAFE_SUBJECT = /^[\w:+-]{1,64}$/;

/* Anonymous, aggregate usage counters. A data point is only
   (event, subject, milliseconds) — no cookies, no IP, no user id, nothing
   that could identify a person or follow them across sites. `open` and
   `filter` come from the client beacon, `download` is counted here at the
   /dl/ route itself so it also covers hotlinks and middle-clicks. */
function record(env, event, subject, ms) {
  if (!env.USAGE) return;          // binding absent until the dataset is created
  try {
    env.USAGE.writeDataPoint({
      blobs: [event, subject],
      doubles: [Math.max(0, Math.min(600000, Number(ms) || 0))],
      indexes: [event],
    });
  } catch {
    /* metrics must never break the site */
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/event") {
      if (request.method !== "POST") return new Response(null, { status: 405 });
      let body;
      try {
        const text = await request.text();
        if (text.length > 300) return new Response(null, { status: 413 });
        body = JSON.parse(text);
      } catch {
        return new Response(null, { status: 400 });
      }
      const subject = String(body.id || "");
      if (!EVENTS.has(body.e) || (subject && !SAFE_SUBJECT.test(subject))) {
        return new Response(null, { status: 400 });
      }
      record(env, body.e, subject, body.ms);
      return new Response(null, { status: 204 });
    }

    const m = url.pathname.match(/^\/dl\/([^/]+)$/);

    if (m && SAFE_NAME.test(m[1])) {
      const name = m[1];
      const base = (env.R2_PUBLIC_BASE || "https://cdn.okeyamy.xyz").replace(/\/$/, "");
      const upstream = await fetch(`${base}/wallpapers/${name}`);
      if (!upstream.ok) {
        return new Response("Not found", { status: upstream.status === 404 ? 404 : 502 });
      }
      record(env, "download", name.split(".")[0], 0);
      // Content-Disposition forces the save; the download attribute alone
      // would have worked too since this is now same-origin, but the header
      // is the more robust trigger and survives a hotlinked /dl/ URL as well.
      const headers = new Headers(upstream.headers);
      headers.set("Content-Disposition", `attachment; filename="${name}"`);
      headers.set("Cache-Control", "public, max-age=31536000, immutable");
      return new Response(upstream.body, { status: 200, headers });
    }

    return env.ASSETS.fetch(request);
  },
};
