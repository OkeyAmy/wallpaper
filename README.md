# wallpapers.okeyamy.xyz

A self-updating anime wallpaper archive. A scheduled job reads curated
subreddits daily, filters and re-encodes what it finds, extracts a colour
palette from each image, and commits the result. Cloudflare Pages serves the
repo as a static site — there is no server, no database, and no build step.

```
GitHub Actions (daily cron)
  └─ sync_reddit.py    fetch → filter → dedupe → WebP → palette
  └─ build_site.py     manifest + JSON-LD + sitemap + robots + llms.txt + OG card
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

Set these and the pipeline uses R2; leave them unset and it falls back to
writing into `wallpapers/` so the project still runs with no cloud account:

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
- NSFW-flagged, video and gallery posts are skipped

## Reddit credentials

Anonymous access works locally but Reddit generally blocks the public JSON
endpoints from cloud IPs, so GitHub Actions needs OAuth. Create a **script**
app at https://www.reddit.com/prefs/apps, then add four repository secrets
(Settings → Secrets and variables → Actions):

`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`

Without them the workflow still runs; it just tends to get rate-limited.

## Deploying

Hosting is **Cloudflare Pages**, not Vercel: static egress is unmetered, which
is the entire cost model of a wallpaper site. Vercel's Hobby tier caps
bandwidth at 100 GB/month and disallows commercial use.

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

## SEO / AI discoverability

The gallery renders client-side, so the build step injects content crawlers
can actually read:

- A static `<a>` + `<img>` gallery inside `#grid`, replaced by the interactive
  grid once JS loads (and left in place if the index fails to load)
- JSON-LD `ImageGallery` (every item, with dimensions and credit) and `FAQPage`
- `sitemap.xml`, `robots.txt`, and `llms.txt` — a plain-text brief for AI crawlers
- Per-wallpaper deep links (`/#w-<id>`) that update the page title

## Licence

Site code: MIT. The wallpapers are not covered — copyright remains with the
original artists, and each item links back to its source post. Open an issue
to request removal.
