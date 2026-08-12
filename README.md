# wallpapers.okeyamy.xyz

A self-updating anime wallpaper archive. A scheduled job pulls from curated
sources daily, filters and re-encodes what it finds, extracts a colour
palette from each image, and commits the result. Cloudflare Pages serves the
repo as a static site — there is no server, no database, and no build step.

```
GitHub Actions (daily cron)
  └─ sync_danbooru.py   fetch → filter → dedupe → WebP → palette
  └─ prune.py           drop oldest entries once a free-tier ceiling is hit
  └─ build_site.py      manifest + JSON-LD + sitemap + robots + llms.txt + OG card
  └─ git push
        └─ Cloudflare Pages redeploys automatically
```

## Layout

| Path | What it is |
| --- | --- |
| `index.html` | The whole site. Static gallery is injected at build time. |
| `assets/` | Stylesheet, runtime, favicon, generated OG card |
| `data/wallpapers.json` | The index the frontend reads |
| `wallpapers/` | Processed WebP images — only in local mode; empty once storage is on R2 |
| `incoming/` | Local drop zone for your own uploads (git-ignored) |
| `scripts/` | The pipeline |
| `_headers`, `robots.txt`, `sitemap.xml`, `llms.txt` | Generated — edit `build_site.py`, not these |

## Storage

Images live in **Cloudflare R2**, not in git. The repository therefore holds
only code and a JSON manifest and stays a few megabytes no matter how long
the sync runs — which matters because deleting a file from git does not
reclaim its history, so an image-in-repo archive grows forever.

Set these and the pipeline uses R2. Locally, leaving them unset falls back to
writing into `wallpapers/` so the project still runs with no cloud account —
but the GitHub Actions workflow requires all five and fails its preflight
check without them, rather than silently publishing a manifest that points
at storage nothing was actually uploaded to:

```
R2_ACCOUNT_ID  R2_ACCESS_KEY_ID  R2_SECRET_ACCESS_KEY  R2_BUCKET
R2_PUBLIC_BASE   # public bucket URL, e.g. https://cdn.okeyamy.xyz
```

`R2_PUBLIC_BASE` also drives the `img-src` in the generated CSP, so the
image host can never fall out of sync with the security policy.

### Why this cannot exceed the free tier

`prune.py` runs before every build and drops the oldest entries once either
ceiling is reached:

- **8 GiB stored** — under R2's 10 GB free allowance, with headroom
- **4,000 items** — well under Cloudflare Pages' 20,000-file deploy limit

R2 bills nothing for egress, so traffic alone can never generate a charge.

## Ingest policy

Set in `scripts/pipeline.py`:

- Minimum 1280×720; anything smaller is rejected
- Long edge capped at 3840px
- WebP quality 82, plus a 640px thumbnail
- Deduplicated by SHA-256 **and** by difference hash, so the same image
  reposted at a different size or JPEG quality is caught too
- Rejected as a duplicate before anything is uploaded, not after — a
  content-addressed key means a duplicate would otherwise collide with the
  original's storage location
- Danbooru queries are `rating:general` only

## Adding your own wallpapers

Drop image files into `incoming/` and run `scripts/ingest_local.py`. To credit
wherever an image actually came from, add a same-named `.source` sidecar:

```
incoming/nice.png
incoming/nice.png.source
```

```
https://twitter.com/artist/status/12345
artist_handle
Ganyu standing in the snow
```

Line 1 is the source link, line 2 is the artist/handle (optional), line 3 is a
title (optional). Both files are consumed on ingest — the sidecar is deleted
along with the source image once its contents are written into the item's
`permalink`/`author`/`title` fields.

The title is worth writing. It becomes the image's alt text and the name it is
indexed under, and an image with no title is deliberately left untitled rather
than named after its file — `IMG-20260810-WA0002` describes nothing. Naming the
subject puts it on that character's page:

```bash
python scripts/ingest_local.py --title "Ganyu in the snow" --character Ganyu
```

Both flags apply to every image in the batch; a `.source` sidecar wins over
`--title` for the image it belongs to. `scripts/build_site.py` prints a count of
items it could not describe, so a missing title shows up at build time rather
than in a search result months later.

### Character pages

`build_site.py` writes one page per character into `w/`, served at
`/w/<character>`, for every character with at least `MIN_HUB_ITEMS` wallpapers
(2). Characters below the line still appear on the homepage and in the image
sitemap — they just don't get a page whose only content is a single image.

`scripts/backfill_metadata.py` is a one-off that repaired the titles and
character tags of items ingested before any of this existed. It has already
been run; it is kept because it is idempotent and documents what was wrong.

## Deploying

Static egress on Cloudflare Pages is unmetered, which matters because a
wallpaper site's entire cost is bandwidth.

1. Push this repo to GitHub (public — Actions minutes are unlimited on public repos).
2. R2 → Create bucket. Add an R2 API token (Object Read & Write) and set the
   five `R2_*` values as repository secrets.
3. Give the bucket a public URL: either enable its `r2.dev` domain or attach a
   custom one such as `cdn.okeyamy.xyz`. That value is `R2_PUBLIC_BASE`.
4. Cloudflare Pages → Create project → connect the repo.
   Build command: *none*. Output directory: `/`.
5. Custom domain → `wallpapers.okeyamy.xyz`.

### Anti-scraping

`robots.txt` welcomes search and AI crawlers (that is the point — discovery)
while keeping generic bots out of the image directories. That is advisory
only, so the actual defence is at the edge — in the Cloudflare dashboard:

- **Security → Bots →** enable Bot Fight Mode
- **Security → WAF → Rate limiting rules →** add a rule on
  `http.request.uri.path contains "/wallpapers/"`, e.g. 60 requests per minute
  per IP, action *Block*

That breaks the sequential crawl pattern used to clone a whole gallery without
affecting anyone browsing normally.

## Licence

Site code: MIT. The wallpapers are not covered — copyright remains with the
original artists, and each item links back to its source post. Open an issue
to request removal.
