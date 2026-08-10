/* ═══════════════════════════════════════════════════════════════════
   WALLPAPER ARCHIVE ── runtime
   Zero dependencies. Reads /data/wallpapers.json, renders, filters,
   and drives an origin-anchored lightbox.
   ═══════════════════════════════════════════════════════════════════ */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const PAGE = 24;                       // cells per progressive batch
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)');

const HUES = [
  { id: 'red',    label: 'RED',    range: [345, 15],  swatch: '#C0392B' },
  { id: 'orange', label: 'ORANGE', range: [15, 45],   swatch: '#D2792B' },
  { id: 'yellow', label: 'YELLOW', range: [45, 70],   swatch: '#C9B037' },
  { id: 'green',  label: 'GREEN',  range: [70, 165],  swatch: '#3E8E5A' },
  { id: 'cyan',   label: 'CYAN',   range: [165, 200], swatch: '#2E8B96' },
  { id: 'blue',   label: 'BLUE',   range: [200, 255], swatch: '#3A5FA8' },
  { id: 'purple', label: 'PURPLE', range: [255, 290], swatch: '#6B4B9E' },
  { id: 'pink',   label: 'PINK',   range: [290, 345], swatch: '#B5487F' },
  { id: 'mono',   label: 'MONO',   range: null,       swatch: '#8A8A8A' },
];

const state = {
  all: [],
  view: [],
  shown: 0,
  ratio: 'all',
  hues: new Set(),
  q: '',
  sort: 'new',
  lbIndex: -1,
  seed: Date.now(),
};

/* ── colour utils ────────────────────────────────────────────── */
function hexToHsl(hex) {
  const n = parseInt(hex.replace('#', ''), 16);
  const r = ((n >> 16) & 255) / 255, g = ((n >> 8) & 255) / 255, b = (n & 255) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  const l = (max + min) / 2;
  if (!d) return { h: 0, s: 0, l };
  const s = d / (1 - Math.abs(2 * l - 1));
  let h;
  if (max === r)      h = 60 * (((g - b) / d) % 6);
  else if (max === g) h = 60 * ((b - r) / d + 2);
  else                h = 60 * ((r - g) / d + 4);
  return { h: (h + 360) % 360, s, l };
}

function hueBucket(hex) {
  const { h, s } = hexToHsl(hex);
  if (s < 0.16) return 'mono';
  for (const t of HUES) {
    if (!t.range) continue;
    const [a, b] = t.range;
    if (a > b ? (h >= a || h < b) : (h >= a && h < b)) return t.id;
  }
  return 'mono';
}

/* ── misc utils ──────────────────────────────────────────────── */
const bytes = n => {
  if (!n) return '—';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
};

const ratioClass = (w, h) => {
  const r = w / h;
  if (r >= 2.2) return 'uw';
  if (r >= 1.2) return 'wide';
  return 'tall';
};

// deterministic shuffle so a re-render inside one session keeps its order
function seededShuffle(arr, seed) {
  const a = [...arr];
  let s = seed;
  const rnd = () => (s = (s * 1664525 + 1013904223) % 4294967296) / 4294967296;
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rnd() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

const debounce = (fn, ms) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

/* ── boot ────────────────────────────────────────────────────── */
async function boot() {
  $('#year').textContent = new Date().getFullYear();

  let data;
  try {
    const res = await fetch('/data/wallpapers.json', { cache: 'no-cache' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (err) {
    fail(err);
    return;
  }

  state.all = (data.items || []).map((it, i) => ({
    ...it,
    _i: i,
    _ratio: ratioClass(it.w, it.h),
    _hue: hueBucket((it.palette && it.palette[0]) || '#808080'),
    _hay: [it.title, it.sub, it.author, ...(it.tags || []), ...(it.palette || [])]
      .filter(Boolean).join(' ').toLowerCase(),
  }));

  paintMeta(data);
  buildHues();
  wire();
  apply();
  openFromHash();
}

// The static gallery baked into index.html by build_site.py is left in place
// on failure, so a broken index degrades to the crawlable fallback rather
// than to an empty page.
function fail(err) {
  $('#grid').setAttribute('aria-busy', 'false');
  $('#loadmsg').innerHTML =
    `<span style="color:var(--red-hot)">── LIVE INDEX UNREACHABLE ──</span><br>` +
    `<span class="dimmer" style="text-transform:none">${escapeHtml(err.message || err)} · showing static index</span>`;
  $('#resultLine').textContent = 'stderr ── falling back to static index';
  $('#cmd').setAttribute('aria-disabled', 'true');
}

function paintMeta(data) {
  const n = state.all.length;
  const disk = state.all.reduce((s, x) => s + (x.bytes || 0), 0);
  const built = data.generated ? new Date(data.generated) : null;

  $('#statCount').innerHTML = `<span class="dim">UNITS</span> ${String(n).padStart(3, '0')}`;
  $('#statSync').innerHTML =
    `<span class="dim">SYNC</span> ${built ? built.toISOString().slice(0, 10) : '—'}`;
  $('#fetchIndex').textContent  = `${n} units / ${new Set(state.all.map(x => x.sub)).size} feeds`;
  $('#fetchDisk').textContent   = bytes(disk);
  $('#footBuild').textContent   = built ? built.toISOString().replace('T', ' ').slice(0, 16) + 'Z' : '—';

  if (built) {
    const tick = () => {
      const s = Math.max(0, (Date.now() - built.getTime()) / 1000);
      const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
      $('#fetchUptime').textContent = `${d}d ${h}h ${m}m since sync`;
    };
    tick();
    setInterval(tick, 30000);
  }
}

function buildHues() {
  const live = new Set(state.all.map(x => x._hue));
  $('#hues').innerHTML = HUES.filter(h => live.has(h.id)).map(h =>
    `<button class="hue" data-hue="${h.id}" title="${h.label}" aria-pressed="false"
             style="background:${h.swatch}"><span class="sr">${h.label}</span></button>`
  ).join('');
}

/* ── filter + sort ───────────────────────────────────────────── */
function apply() {
  const q = state.q.trim().toLowerCase();
  let v = state.all.filter(x =>
    (state.ratio === 'all' || x._ratio === state.ratio) &&
    (!state.hues.size || state.hues.has(x._hue)) &&
    (!q || x._hay.includes(q))
  );

  if (state.sort === 'new')      v.sort((a, b) => (b.added || '').localeCompare(a.added || '') || a._i - b._i);
  else if (state.sort === 'res') v.sort((a, b) => (b.w * b.h) - (a.w * a.h));
  else                           v = seededShuffle(v, state.seed);

  state.view = v;
  state.shown = 0;
  $('#grid').innerHTML = '';
  $('#grid').setAttribute('aria-busy', 'false');

  const filtered = state.ratio !== 'all' || state.hues.size || q;
  $('#clearAll').hidden = !filtered;
  $('#empty').hidden = v.length > 0;
  $('#resultLine').textContent = v.length
    ? `stdout ── ${v.length} of ${state.all.length} units matched`
    : `stdout ── 0 units matched`;

  more();
}

function more() {
  const slice = state.view.slice(state.shown, state.shown + PAGE);
  if (!slice.length) {
    $('#loadmsg').hidden = true;
    return;
  }

  const frag = document.createDocumentFragment();
  slice.forEach((it, i) => frag.appendChild(cell(it, state.shown + i, i)));
  $('#grid').appendChild(frag);
  state.shown += slice.length;
  $('#loadmsg').hidden = state.shown >= state.view.length;
}

function cell(it, absIndex, stagger) {
  const el = document.createElement('button');
  el.className = 'cell';
  el.type = 'button';
  el.setAttribute('role', 'listitem');
  el.dataset.index = absIndex;
  // stagger is decorative and capped — a long cascade reads as slow, not alive
  el.style.setProperty('--d', String(Math.min(stagger, 11)));
  el.setAttribute('aria-label', `${it.title || 'Wallpaper'} — ${it.w}×${it.h}`);

  const pal = (it.palette || []).slice(0, 5)
    .map(c => `<i style="background:${safeHex(c)}"></i>`).join('');

  el.innerHTML = `
    <img class="cell__img" loading="lazy" decoding="async"
         src="${escapeAttr(safePath(it.thumb))}" alt="${escapeAttr(it.title || 'Wallpaper')}"
         width="${Number(it.w) || 0}" height="${Number(it.h) || 0}">
    <span class="cell__reg">+</span>
    <span class="cell__pal">${pal}</span>
    <span class="cell__hud">
      <span style="min-width:0">
        <span class="cell__name">${escapeHtml(it.title || 'UNTITLED')}</span><br>
        <span class="cell__sub">${escapeHtml(it.sub ? 'r/' + it.sub : it.source || 'LOCAL')}</span>
      </span>
      <span class="cell__res">${Number(it.w) || 0}×${Number(it.h) || 0}</span>
    </span>`;

  const img = el.firstElementChild;
  const ready = () => { img.classList.add('is-loaded'); el.classList.add('is-ready'); };
  if (img.complete && img.naturalWidth) ready();
  else {
    img.addEventListener('load', ready, { once: true });
    img.addEventListener('error', () => {
      el.classList.add('is-ready');
      img.replaceWith(Object.assign(document.createElement('span'), {
        className: 'cell__err',
        style: 'display:grid;place-items:center;height:100%;color:var(--dimmer);font-size:10px',
        textContent: '⚠ ASSET MISSING',
      }));
    }, { once: true });
  }

  el.addEventListener('click', () => openLb(absIndex, el));
  return el;
}

/* Manifest values originate from Reddit post titles and authors, i.e. from
   untrusted input. Everything interpolated into markup goes through one of
   these three gates: text, colour literal, or same-origin asset path. */
const escapeHtml = s => String(s).replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const escapeAttr = escapeHtml;

// only ever emit a literal hex colour into a style attribute
const safeHex = c => (/^#[0-9a-fA-F]{6}$/.test(String(c)) ? String(c) : '#808080');

// asset paths must stay relative and same-origin — no protocol, no host hop
const safePath = p => {
  const s = String(p || '');
  return /^[\w./-]+$/.test(s) && !s.includes('..') ? s : '';
};

// external links: http(s) only, so a crafted permalink can't become javascript:
const safeUrl = u => {
  // an empty string would resolve against location.origin and come back as
  // the page's own URL, so reject falsy input before parsing
  if (!u) return '';
  try {
    const url = new URL(String(u), location.origin);
    return /^https?:$/.test(url.protocol) ? url.href : '';
  } catch { return ''; }
};

/* ── lightbox ────────────────────────────────────────────────── */
const lb = $('#lb');
let lastFocus = null;

function openLb(i, sourceEl) {
  const it = state.view[i];
  if (!it) return;
  state.lbIndex = i;
  lastFocus = sourceEl || document.activeElement;

  // anchor the scale origin to the tile that opened it, so the image
  // grows out of where it came from and collapses back to the same point.
  // Clamped to the viewport: a tile scrolled off-screen would otherwise put
  // the origin outside the frame and the image would appear to fly in from
  // nowhere instead of growing out of the thumbnail.
  if (sourceEl && !REDUCED.matches) {
    const r = sourceEl.getBoundingClientRect();
    const clamp = v => Math.max(0, Math.min(100, v));
    lb.style.setProperty('--ox', `${clamp((r.left + r.width / 2) / innerWidth * 100)}%`);
    lb.style.setProperty('--oy', `${clamp((r.top + r.height / 2) / innerHeight * 100)}%`);
  } else {
    lb.style.setProperty('--ox', '50%');
    lb.style.setProperty('--oy', '50%');
  }

  paintLb(it, i);
  lb.hidden = false;
  document.body.classList.add('lb-open');
  requestAnimationFrame(() => lb.classList.add('is-open'));
  $('#lbNext').focus({ preventScroll: true });
}

/* Deep links: /#w-<id> opens straight to one wallpaper, so a shared link
   lands on the item instead of the top of the gallery. */
function syncHash(it) {
  const want = it ? `#w-${it.id}` : '';
  if (location.hash !== want) history.replaceState(null, '', want || location.pathname);
  if (it) {
    document.title = `${it.title || 'Wallpaper'} — ${it.w}×${it.h} anime wallpaper | okeyamy`;
  } else {
    document.title = 'Anime Wallpaper Archive — 4K & Phone Wallpapers, Palette-Indexed | okeyamy';
  }
}

function openFromHash() {
  const m = /^#w-([a-f0-9]{6,32})$/.exec(location.hash);
  if (!m) return;
  const i = state.view.findIndex(x => x.id === m[1]);
  if (i < 0) return;
  while (state.shown <= i && state.shown < state.view.length) more();
  const target = $(`.cell[data-index="${i}"]`);
  // bring the tile on-screen first so the lightbox has a real origin to
  // grow from, and so closing returns the user to the item they arrived at
  target?.scrollIntoView({ block: 'center', behavior: 'instant' });
  openLb(i, target);
}

function paintLb(it, i) {
  const img = $('#lbImg');
  const full = safePath(it.file);
  img.classList.remove('is-loaded');
  img.src = full;
  img.alt = it.title || 'Wallpaper';

  $('#lbIdx').textContent   = `${String(i + 1).padStart(2, '0')}/${String(state.view.length).padStart(2, '0')}`;
  $('#lbTitle').textContent = it.title || 'UNTITLED';
  $('#lbSpec').textContent  = `${it.w}×${it.h} · ${bytes(it.bytes)} · ${it._ratio.toUpperCase()}`;

  const rows = [
    ['RESOLUTION', `${it.w} × ${it.h}`],
    ['ASPECT',     it.ratio || (it.w / it.h).toFixed(2)],
    ['SIZE',       bytes(it.bytes)],
    ['SOURCE',     it.sub ? `r/${it.sub}` : (it.source || 'local ingest')],
    ['POSTER',     it.author ? `u/${it.author}` : '—'],
    ['INDEXED',    it.added || '—'],
    ['HUE',        it._hue.toUpperCase()],
  ];
  $('#lbMeta').innerHTML = rows.map(([k, v]) =>
    `<div><dt>${k}</dt><dd>${escapeHtml(v)}</dd></div>`).join('');

  $('#lbPal').innerHTML = (it.palette || []).map(c => {
    const hex = safeHex(c);
    return `<button style="background:${hex}" data-hex="${hex}" title="Copy ${hex}">${hex.slice(1)}</button>`;
  }).join('');

  const dl = $('#lbDl');
  dl.href = full;
  dl.setAttribute('download', `${String(it.id || 'wallpaper').replace(/[^\w-]/g, '')}.${full.split('.').pop() || 'webp'}`);

  const src = $('#lbSrc');
  const perma = safeUrl(it.permalink);
  if (perma) { src.href = perma; src.hidden = false; }
  else { src.removeAttribute('href'); src.hidden = true; }

  const done = () => img.classList.add('is-loaded');
  if (img.complete && img.naturalWidth) done();
  else img.addEventListener('load', done, { once: true });

  syncHash(it);
}

function closeLb() {
  if (lb.hidden) return;
  lb.classList.remove('is-open');
  document.body.classList.remove('lb-open');
  const end = () => { lb.hidden = true; $('#lbImg').removeAttribute('src'); };
  REDUCED.matches ? end() : setTimeout(end, 260);
  lastFocus?.focus?.({ preventScroll: true });
  state.lbIndex = -1;
  syncHash(null);
}

function stepLb(dir) {
  if (state.lbIndex < 0) return;
  const n = state.view.length;
  const next = (state.lbIndex + dir + n) % n;
  state.lbIndex = next;
  paintLb(state.view[next], next);
  // pull the next page in if paging off the end of what's rendered
  while (state.shown <= next && state.shown < state.view.length) more();
}

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    const was = btn.textContent;
    btn.textContent = '✓ COPIED';
    setTimeout(() => { btn.textContent = was; }, 1200);
  } catch {
    /* clipboard blocked (insecure context / denied) — fail quiet, not broken */
  }
}

/* ── wiring ──────────────────────────────────────────────────── */
function wire() {
  $$('.wsp__i').forEach(b => b.addEventListener('click', () => {
    $$('.wsp__i').forEach(x => x.classList.toggle('is-on', x === b));
    state.ratio = b.dataset.ratio;
    apply();
  }));

  $('#hues').addEventListener('click', e => {
    const b = e.target.closest('.hue');
    if (!b) return;
    const id = b.dataset.hue;
    state.hues.has(id) ? state.hues.delete(id) : state.hues.add(id);
    b.classList.toggle('is-on', state.hues.has(id));
    b.setAttribute('aria-pressed', String(state.hues.has(id)));
    apply();
  });

  $('#q').addEventListener('input', debounce(e => {
    state.q = e.target.value;
    apply();
  }, 120));

  $$('.sortbtn').forEach(b => b.addEventListener('click', () => {
    $$('.sortbtn').forEach(x => x.classList.toggle('is-on', x === b));
    state.sort = b.dataset.sort;
    if (state.sort === 'rand') state.seed = Date.now();
    apply();
  }));

  const reset = () => {
    state.ratio = 'all'; state.hues.clear(); state.q = '';
    $('#q').value = '';
    $$('.wsp__i').forEach(x => x.classList.toggle('is-on', x.dataset.ratio === 'all'));
    $$('.hue').forEach(x => { x.classList.remove('is-on'); x.setAttribute('aria-pressed', 'false'); });
    apply();
  };
  $('#clearAll').addEventListener('click', reset);
  $('#emptyReset').addEventListener('click', reset);

  lb.addEventListener('click', e => { if (e.target.closest('[data-close]')) closeLb(); });
  $('#lbPrev').addEventListener('click', () => stepLb(-1));
  $('#lbNext').addEventListener('click', () => stepLb(1));
  $('#lbCopy').addEventListener('click', e => {
    const it = state.view[state.lbIndex];
    if (it) copyText((it.palette || []).join('\n'), e.currentTarget);
  });
  $('#lbPal').addEventListener('click', e => {
    const b = e.target.closest('[data-hex]');
    if (b) copyText(b.dataset.hex, $('#lbCopy'));
  });

  // infinite scroll
  new IntersectionObserver(entries => {
    if (entries.some(e => e.isIntersecting)) more();
  }, { rootMargin: '900px 0px' }).observe($('#sentinel'));

  document.addEventListener('keydown', onKey);
}

function onKey(e) {
  const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName);

  if (e.key === 'Escape') {
    if (!lb.hidden) closeLb();
    else if (typing) document.activeElement.blur();
    else if (!$('#clearAll').hidden) $('#clearAll').click();
    return;
  }

  if (!lb.hidden) {
    if (e.key === 'ArrowRight' || e.key === 'l') { e.preventDefault(); stepLb(1); }
    if (e.key === 'ArrowLeft'  || e.key === 'h') { e.preventDefault(); stepLb(-1); }
    if (e.key === 'd') $('#lbDl').click();
    return;
  }

  if (typing) return;

  if (e.key === '/') { e.preventDefault(); $('#q').focus(); return; }
  if (e.key === 'r') { $('.sortbtn[data-sort="rand"]')?.click?.(); return; }
  if (['1', '2', '3', '4'].includes(e.key)) {
    $$('.wsp__i')[Number(e.key) - 1]?.click();
  }
}

boot();
