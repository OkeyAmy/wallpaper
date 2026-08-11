// Entry point for the Worker deploy (see wrangler.toml). Everything except
// the one dynamic route below is a static asset served straight from the
// repo — this Worker exists only to add /dl/:name.
const SAFE_NAME = /^[a-f0-9]{6,32}\.(webp|jpg|jpeg|png)$/;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const m = url.pathname.match(/^\/dl\/([^/]+)$/);

    if (m && SAFE_NAME.test(m[1])) {
      const name = m[1];
      const base = (env.R2_PUBLIC_BASE || "https://cdn.okeyamy.xyz").replace(/\/$/, "");
      const upstream = await fetch(`${base}/wallpapers/${name}`);
      if (!upstream.ok) {
        return new Response("Not found", { status: upstream.status === 404 ? 404 : 502 });
      }
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
