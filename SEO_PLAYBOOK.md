# Notare SEO Playbook

Working doc. Tracks what's implemented in the site and what **you** need to do off-site
(directories, Search Console, Cloudflare). Domain: **notarelegal.com** (Cloudflare → Render → Git).

Rule we hold to: **never name a competitor directly**, and **never overclaim** (local vs cloud,
accuracy, pricing). All copy below follows both.

---

## 1. What's already implemented (staged in this repo — deploy with a normal git push)

**On-page SEO**
- `site.html` (home): tuned title, meta description, canonical, Open Graph + Twitter cards, JSON-LD `SoftwareApplication` schema (price range $50–$200 pulled from the live pricing so it can't mismatch).
- `about / download / contact / help / terms / privacy`: full title + description + canonical + OG/Twitter, set to **index**.
- App + transactional pages (editor, live, new_job, cases, profiles, settings, checkout, thank-you, index/OTA, admin, etc.): set to **noindex, nofollow** so only marketing pages get indexed.

**New landing pages (target the head searches without naming anyone)**
- `court-reporting-software.html` → "court reporting software", "digital court reporting", "real-time reporting"
- `legal-transcription-software.html` → "legal transcription software", "deposition transcription"
- `alternative.html` → "court reporting / legal transcription software alternative", "switching" intent
- Each has a FAQ section with `FAQPage` JSON-LD (eligible for rich results).

**Technical**
- `robots.txt` (root) — allows crawl, blocks /api/ + admin/checkout/thank-you, points to sitemap.
- `sitemap.xml` (root, 10 URLs) — served via new server routes `/robots.txt` and `/sitemap.xml`.
- `og-notare.png` — 1200×630 branded social card (so shared links render a preview).

**Deploy:** `git push` to the update_server repo → Render rebuilds. Cloudflare front is unchanged.

---

## 2. Google Search Console + Bing — DO THIS FIRST (highest priority)

None of the on-page work matters until search engines know the site exists.

1. **Google Search Console** → add property `notarelegal.com` (Domain property).
   - Verify with a **DNS TXT record in Cloudflare** (GSC gives you the value; add it under Cloudflare DNS → it's instant).
   - Submit `https://notarelegal.com/sitemap.xml`.
   - Use **URL Inspection → Request indexing** on the home page and the 3 new landing pages.
2. **Bing Webmaster Tools** → add the site, import from GSC (one click), submit the same sitemap.
3. Set the **preferred domain** in Cloudflare with a redirect rule (see §4) so only one version is indexed.

---

## 3. Directory submission kit (off-page — drives qualified buyers, often outranks our own site)

For each: free listing, needs a business email + a few screenshots + the blurbs below.
Submit Notare's own profile — these are not "vs" pages, just our listing.

| Directory | Category to file under | Notes |
|---|---|---|
| **Capterra** (capterra.com/vendors) | Legal / Transcription / Court reporting | Biggest B2B-software directory; feeds GetApp + Software Advice (all Gartner). One submission can populate all three. |
| **GetApp** | same (via Gartner Digital Markets) | Auto-populated from Capterra in many cases. |
| **Software Advice** | same (via Gartner Digital Markets) | Same. |
| **G2** (g2.com — "Improve your listing") | Court Reporting Services / Transcription | Review-driven; ask 3–5 friendly users (Beth's network) to leave honest reviews after listing. |
| **LawNext Directory** (directory.lawnext.com) | **Court Reporting** | Niche legal-tech directory — high-relevance backlink, low competition. Strong fit. |
| **TrustRadius** | Transcription / Legal software | Secondary; do after the above. |

### Reusable boilerplate (copy/paste; honest, no names)

**Product name:** Notare
**Vendor:** Legacy Transcription Services, LLC
**Category:** Legal transcription & court reporting software
**Platform:** Windows (desktop)

**One-liner (≤90 chars):**
> Local-first legal transcription and court reporting software — capture to finished transcript.

**Short description (≤160 chars):**
> Notare turns audio into finished legal transcripts: AI transcription, editing, proofreading and real-time reporting in one local-first Windows suite. Flat per-user pricing.

**Long description (~400 chars):**
> Notare is legal transcription and court reporting software built for the way reporters and
> transcribers actually work. It follows the record from live capture and real-time draft
> through automatic transcription, legal-formatting cleanup, proofreading, and delivery — five
> workspaces you can use on their own or as one connected pipeline. It runs on your Windows
> machine: transcripts, profiles and case files stay local, and you choose an on-device engine
> or bring your own cloud recognition key. Flat per-user pricing, no per-minute fees.

**Key features (bullet list for directory forms):**
- Live capture and real-time draft for digital court reporting
- Automatic transcription (on-device engine or bring-your-own cloud key)
- Legal-formatting editor (capitalization, spacing, standard rules)
- Proofreading profiles tuned for legal transcripts
- Reporter certification and transcript delivery
- Local-first: your record stays on your machine
- Flat per-user pricing — no per-minute charges, no transcript limits

**Differentiators (for "why choose" fields — phrase generically, never name competitors):**
- Flat per-user pricing instead of per-minute metering
- Pay only for the workspaces you use (à la carte or bundle)
- Local-first desktop app — no forced cloud
- Bring-your-own recognition engine

**Assets to have ready:** logo (quill mark), 3–5 screenshots (picker/workspaces, editor, real-time
view, delivery), the `og-notare.png` card, pricing summary, support email/URL.

---

## 4. Cloudflare technical wins (do after deploy)

- **Canonical host:** add a redirect rule `www.notarelegal.com/* → notarelegal.com/$1` (301) so only the apex is indexed. Confirm HTTP→HTTPS is on (Cloudflare "Always Use HTTPS").
- **Clean URLs (nice-to-have):** Cloudflare Transform/Redirect rule so `/court-reporting-software`,
  `/legal-transcription-software`, `/about`, `/download` resolve to the `/static/*.html` files.
  Cleaner URLs rank slightly better and look more trustworthy. If you do this, update the
  canonicals in those files to match.
- **Cache:** let Cloudflare cache the marketing HTML (the server already sends no-cache on `.html`
  for the admin app — fine; Cloudflare can still edge-cache the public pages with a short TTL).
- **Core Web Vitals:** confirm Render isn't cold-starting the marketing page (paid plan = no spin-down).

---

## 5. Keyword map (what each page targets)

| Page | Primary | Secondary |
|---|---|---|
| Home | legal transcription software | court reporting software |
| court-reporting-software | court reporting software | digital court reporting, real-time reporting |
| legal-transcription-software | legal transcription software | deposition transcription, hearing transcription |
| alternative | court reporting software alternative | legal transcription software alternative, switch |

---

## 6. Next content (optional, top-of-funnel — when you want to invest more)

Informational posts that attract reporters/firms searching how-to (then link to the product pages).
All honest, no competitor names:
- "How digital court reporters turn audio into a finished transcript"
- "Per-minute vs flat pricing for transcription: what it actually costs"
- "Keeping the record local: on-device vs cloud transcription"
- "Setting up proofreading rules for legal transcripts"

A `/resources/` or `/blog/` section, each post 800–1,200 words, internally linked to the three
landing pages. This is the slow-compounding layer; the foundation + directories above come first.
