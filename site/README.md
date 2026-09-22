# Website (site/)

Client-facing product site: React 19 + Vite + TypeScript, deployed to Netlify as static
files. No dependency beyond React — routing is a 40-line file (`src/router.tsx`).

## Run it

```bash
cd site
npm install
cp .env.example .env.local        # optional: set VITE_BRAIN_API_URL for live demo search
npm run dev                       # http://localhost:5173
npm run build                     # type-checks, then writes dist/
```

## Where to edit

| What | File |
|---|---|
| Company name, email, tagline | `src/config.ts` |
| Products, status badges, copy | `src/data/products.ts` |
| Colours, fonts, spacing | top of `src/styles.css` (light + dark tokens) |
| Privacy notice (template — get it reviewed) | `src/pages/Legal.tsx` |
| Security headers, CSP, SPA redirect | `../netlify.toml` |

## Deploy to Netlify

1. Push the repo to GitHub and "Import from Git" in Netlify. `netlify.toml` at the repo
   root already sets base `site`, build `npm run build`, publish `dist`.
2. Site settings → Environment variables: `VITE_BRAIN_API_URL=https://brain.<your-domain>`.
3. Edit `connect-src` in `netlify.toml`'s Content-Security-Policy to the same hostname.
4. Forms → enable form detection. The contact form posts to Netlify Forms; the hidden
   copy in `index.html` is what registers it, so keep field names in sync.

## Demo search → brain API

The Demo page calls `GET /public/search?q=` on the brain directly from the browser
(not via Netlify rewrites, which would burn bandwidth credits). On the Debian box:

```
BRAIN_PUBLIC_DEMO=1
BRAIN_CORS_ORIGINS=https://<your-site>.netlify.app,https://<your-domain>
```

That endpoint is locked down in code: Stack Exchange / OSGeo / GitHub only, never
Discord, NAS files or NightArc; anything tagged `sensitive` or `has-grid-ref` dropped;
short excerpts with author + link for CC BY-SA attribution; per-IP rate limit. Add a
Cloudflare rate-limiting rule in front of it as well.

Without `VITE_BRAIN_API_URL` the page shows a clearly labelled sample result.
