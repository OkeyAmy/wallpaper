// Cloudflare Pages Function: same-origin download proxy for R2 objects.
//
// The browser's `download` attribute is spec-ignored on cross-origin links,
// and fetch()-then-blob needs the R2 bucket to send CORS headers (it
// doesn't, by default). Proxying through a same-origin route sidesteps both:
// no CORS config needed, no whole-file buffering in page memory for a large
// wallpaper, and Content-Disposition forces the save even without the
// `download` attribute.
const SAFE_NAME = /^[a-f0-9]{6,32}\.(webp|jpg|jpeg|png)$/;
const R2_PUBLIC_BASE = "https://cdn.okeyamy.xyz";

export async function onRequestGet({ params, env }) {
  const name = params.name;
  if (!SAFE_NAME.test(name)) {
    return new Response("Not found", { status: 404 });
  }

  const base = (env.R2_PUBLIC_BASE || R2_PUBLIC_BASE).replace(/\/$/, "");
  const upstream = await fetch(`${base}/wallpapers/${name}`);
  if (!upstream.ok) {
    return new Response("Not found", { status: upstream.status === 404 ? 404 : 502 });
  }

  const headers = new Headers(upstream.headers);
  headers.set("Content-Disposition", `attachment; filename="${name}"`);
  headers.set("Cache-Control", "public, max-age=31536000, immutable");
  return new Response(upstream.body, { status: 200, headers });
}
