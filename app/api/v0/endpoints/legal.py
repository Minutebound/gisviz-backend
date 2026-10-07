"""
app/api/v1/endpoints/legal.py
──────────────────────────────
Legal page content served from Redis.
No database table — admin sets content via PUT, reads are cached 30 days.
Falls back to hardcoded defaults when Redis is cold so pages always render.

Routes:
  GET  /legal/         — list available slugs (public)
  GET  /legal/{slug}   — get content (public, Redis-cached)
  PUT  /legal/{slug}   — update content (admin only, busts cache)
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db.models import PlatformUserRecord
from app.services.auth_service import RoleChecker
from app.services.cache_service import cache_service

router = APIRouter()

VALID_SLUGS = {"about", "privacy", "terms", "cookies", "accessibility", "data-policy"}
# Version of the terms / privacy policy users accept at sign-up (consent_records). Bump it when they change.
LEGAL_VERSION = "2026-10-04"
_TTL = 86400 * 30  # 30 days


_DEFAULTS: dict[str, dict] = {

    # ── ABOUT ────────────────────────────────────────────────────────────────
    "about": {
        "title": "About GisViz",
        "last_updated": "July 2026",
        "content": """\
GisViz is an open platform for sharing geospatial data visualizations, maps, and spatial analytics.

This is a small team of graduate with one leading to fullfill hist idea to build GisViz because for the GIS community — a place where cartographers, data scientists, urban planners, environmental researchers, and location-intelligence engineers could publish their work, discover what others are building, and learn from each other.

── Mission

Our mission is to make geospatial knowledge visible and accessible. Every map tells a story. Every dataset reveals a pattern. GisViz is where those stories get shared.

── What you can do on GisViz

Publish maps and data visualizations with full metadata including data sources, methodology notes, and licensing information. Discover work from the global GIS community through the curated feed and trending posts. Follow publishers whose work you find valuable. Bookmark posts for later reference. Search by category, keyword, or publisher.

── Who we are

GisViz was founded by practitioners who spend their days working with geospatial data. We are a small, independent team. We do not take venture capital. We are not building an advertising business. The product exists to serve the community that uses it.

── Data and privacy

We collect only what we need to operate the service. We do not sell your data. We do not share your data with third parties for advertising purposes. Full details are in our Privacy Policy.

── Contact

Questions, feedback, partnership enquiries, or press requests:
info@gisviz.com

We read everything. Response times vary but we aim to reply within 2 business days.\
""",
    },

    # ── PRIVACY ──────────────────────────────────────────────────────────────
    "privacy": {
        "title": "Privacy Policy",
        "last_updated": "October 2026",
        "content": """\
Effective date: October 4, 2026 (version 2026-10-04)

GisViz ("we", "us", "our") operates gisviz.com. This policy explains what personal data we process, why, on which legal basis, how long we keep it, who receives it, and the rights you have under the EU/UK General Data Protection Regulation (GDPR), the California Consumer Privacy Act (CCPA) and similar laws. When you open an account you confirm that you have read this policy; we keep a record of the version you accepted and when.


1. Who is responsible

GisViz is the controller of the personal data described here. Contact for every privacy question and request: info@gisviz.com.


2. What we process, why, and on which legal basis

Account (contract, Art. 6(1)(b) GDPR)
Email address, handle, password (stored only as a bcrypt hash), account type (individual or organisation), role, verification status, and the record of the terms and privacy policy versions you accepted (consent record). Used to run your account, sign you in and send the emails you need (verification codes, password resets, notifications that something was shared with you).

Profile (contract)
Optional: title, links (LinkedIn, Medium, website), location, avatar and cover photo. Your profile is public, but your email address is never shown on it.

Organisations (contract / legitimate interest, Art. 6(1)(f))
An organisation account is registered with an email address on the organisation's own domain and checked by our team. Once verified, every GisViz user whose verified email address is on that domain becomes a member automatically, so that content shared with the organisation reaches its people. Membership is derived from your email domain and is not shown publicly. If you change your email address, your memberships follow the new domain.

Posts, comments and interactions (contract)
Posts (title, description, notes, sources, categories, keywords, region, the visual's settings and poster design), comments, likes, bookmarks and follows. Public posts are visible to everyone; private posts only to you, the people and organisations you share them with, and our moderators.

Sharing records (contract / legitimate interest)
When a post or dataset is shared, we record who shared it, with whom and when, and who revoked access and when. This history lets owners see who has access and lets us investigate abuse.

Usage and security data (legitimate interest)
A daily view counter per post, keyed by a one-way hash (SHA-256) of your account id, or of your IP address and browser user agent when you are not signed in — the IP address itself is not stored for this. Our servers keep standard request logs (IP address, path, time, status) for up to 30 days for security and debugging. Rate-limit counters (for example for verification codes) are kept for minutes.

Support requests (contract / legitimate interest)
What you write to support and the contact email you give.


3. What we do not do

We do not sell or rent personal data. We do not run behavioural advertising, retargeting, cross-site tracking or fingerprinting, and no third-party analytics service receives your personal data.


4. Datasets and personal data inside them

Datasets are loaded by our team or by their owners. A dataset that contains personal data is flagged as such: its rows are never shown to readers, only aggregated visuals, and it should be private. If you believe a dataset or a visual on GisViz contains personal data about you, write to info@gisviz.com and we will review it promptly, and remove it where the law requires.


5. Recipients and processors

Hosting (IONOS, EU) — servers and databases. Email delivery (IONOS) — transactional emails only. Cloudflare — DNS, content delivery and DDoS protection (processes IP addresses for routing and security).
Your browser also loads a few resources directly from third parties, which therefore see your IP address: map tiles (OpenFreeMap), web fonts (Google Fonts) and country flag icons (flagcdn.com). They receive no account data from us.
We may disclose data if required by law or to protect the safety of users.


6. International transfers

Our servers are in the European Union. Where a provider processes data outside the EEA/UK (for example Cloudflare), the transfer relies on the European Commission's adequacy decisions or Standard Contractual Clauses.


7. How long we keep data

Account and content: as long as your account exists. Deleted account: removed immediately (see 8). Server logs: up to 30 days. Sharing history of a deleted account: kept without personal data ("deleted account"). Admin audit logs (who changed what, by id): kept for security and legal obligations. Backups roll over within 30 days.


8. Your rights

Access and portability — Settings → Privacy → "Download my data" gives you a JSON file with everything we hold about you (account, consents, organisation, posts, comments, likes, bookmarks, follows, shares made and received, support tickets, views recorded under your account).
Rectification — edit your profile and email in Settings.
Erasure — Settings → Privacy → "Delete my account and data" permanently deletes your account, posts (and their images), comments, likes, bookmarks, follows, organisation and memberships. Records of what you shared become anonymous; page views become anonymous.
Restriction and objection — write to us; you may object to processing based on legitimate interest at any time.
Withdrawing consent — where we rely on consent you may withdraw it at any time, without affecting earlier processing.
Complaint — you may lodge a complaint with your data protection authority.
We answer requests within one month. California residents have the rights to know, delete and opt out of sale; we do not sell personal information.


9. Security

Passwords are hashed with bcrypt; connections use TLS; verification codes are random and attempt-limited; sessions can be ended on every device; private visuals are served with short-lived signed links; access to private content is checked on every request.


10. Children

GisViz is not directed at children under 16. We do not knowingly collect their personal data; if you believe a child has an account, contact us and we will delete it.


11. Changes

We will tell registered users about material changes by email before they take effect and show the new version date here.


12. Contact

GisViz — info@gisviz.com\
""",
    },

    # ── TERMS ────────────────────────────────────────────────────────────────
    "terms": {
        "title": "Terms of Service",
        "last_updated": "October 2026",
        "content": """\
Effective date: October 4, 2026 (version 2026-10-04)

These Terms of Service ("Terms") govern your use of GisViz ("we", "us", "our") at gisviz.com (the "Service"). By creating an account you accept these Terms and our Privacy Policy; we keep a record of the version you accepted.


1. The Service

GisViz is a platform for publishing and discovering geospatial and data visualisations. Posts are built from datasets hosted on GisViz:
  — Standard posts use batch datasets, refreshed from time to time when new data is available.
  — Live posts use stream datasets that receive new rows continuously; their visual refreshes itself while you watch, and the time of the last update is shown.
Each post and each dataset is public or private.


2. Accounts

You must be at least 16 years old. You are responsible for your credentials and for everything done with your account. Handles must not impersonate another person or organisation.

Individual accounts belong to one person. Organisation accounts represent an organisation: they must be registered with an email address on the organisation's own domain, are reviewed by us, and may only be opened by someone authorised to act for the organisation. Once verified, users whose verified email is on that domain become members of the organisation and can see what is shared with it. If you leave an organisation, change your email to an address outside its domain.


3. Public and private content, sharing

Public posts appear in the feed, search and your profile. Private posts and private datasets are visible only to their owner, our administrators and moderators, the people and organisations they are shared with, and the members of the organisation that owns them.
When you share something you decide who may see it; you can revoke access at any time. We keep a record of who shared what with whom and when.
A public post may be built on a private dataset only by someone who controls that dataset (its owner, its organisation or an administrator). Readers then see the visual only — never the dataset's rows. If a private dataset was merely shared with you, posts you build on it must stay private.
Do not use sharing to disclose information you are not allowed to disclose.


4. Your content and licence to us

You keep ownership of what you publish. You grant us a non-exclusive, worldwide, royalty-free licence to host, display, reproduce (including as the poster image shown in feeds and link previews) and distribute it to the people allowed to see it, for operating and promoting the Service. For private content the licence covers only the people it is shared with.


5. Data, copyright and attribution

Every dataset carries a licence category, a copyright holder and an attribution text where its licence requires one; GisViz shows the attribution with every post built on it. You must only upload or use data you are entitled to use and respect its licence:
  — Open licences (CC0, PDDL, public domain, CC BY, CC BY-SA, ODC-By, ODbL, OGL): attribution as required; share-alike licences require derived data to keep the same licence.
  — Non-commercial licences (e.g. CC BY-NC): no commercial re-use.
  — "Used with permission" and other restricted licences: readers see the visual only, never the rows; you confirm you hold the permission.
  — "All rights reserved": private use only.
Posts must credit their data source. We may remove content or data that infringes rights; rights holders can contact info@gisviz.com (we act on valid notices, including under the DMCA and the EU Digital Services Act).


6. Personal data in datasets

Do not upload datasets with personal data unless you have a lawful basis to process it and to share it on GisViz. Datasets containing personal data must be flagged as such: their rows are never shown to readers and they should be private. You are the controller of personal data you upload.


7. Stream datasets and ingest keys

Stream datasets receive rows through an ingest key. Keep keys secret; anyone with a key can add rows to that dataset. We may rate-limit or block pushes that harm the Service. Old rows may be dropped by the dataset's retention setting.


8. Prohibited conduct

No unlawful content; no harassment or impersonation; no malware; no spam or automated posting without permission; no attempts to access accounts, private content or systems you are not authorised to; no scraping without written agreement; no sensitive, classified or restricted geospatial data you are not authorised to share.


9. Moderation

We may remove content, revoke shares, suspend accounts or reject organisation registrations that break these Terms. Our administrators and moderators can see private content when needed to run the Service or investigate reports. Report content with the Report button; decisions can be appealed at info@gisviz.com.


10. Ending your account

You can delete your account and personal data at any time in Settings (see the Privacy Policy). We may terminate accounts for serious or repeated violations.


11. Disclaimers and liability

The Service and all user content and data are provided "as is". We do not guarantee the accuracy, completeness or timeliness of any dataset or visual, including live data. To the extent permitted by law we are not liable for indirect or consequential damages, and our total liability is limited to one hundred US dollars (USD 100). Nothing limits liability that cannot be limited by law, including for fraud, gross negligence or wilful misconduct, and nothing affects your statutory rights as a consumer.


12. Governing law

These Terms are governed by the laws of the State of Delaware, USA, without prejudice to mandatory consumer protection rules of the country where you live. If you are a consumer in the EU or UK you may bring claims in your local courts.


13. Changes

We will notify registered users of material changes at least 14 days before they take effect.


14. Contact

GisViz — info@gisviz.com\
""",
    },

    # ── COOKIES ──────────────────────────────────────────────────────────────
    "cookies": {
        "title": "Cookie Policy",
        "last_updated": "October 2026",
        "content": """\
Effective date: October 4, 2026

GisViz uses only what is strictly necessary to run the site. Because we use no analytics, advertising or tracking cookies, no cookie consent banner is needed; if that ever changes we will ask for your consent first.


1. What GisViz stores in your browser (first party, strictly necessary)

gisviz_token (localStorage) — your session, so you stay signed in. Removed when you log out; you can end every session from Settings.
gisviz_handle (localStorage) — your handle, to show the interface without an extra request. Removed when you log out.
Theme preference (localStorage) — light or dark mode.


2. Third parties your browser contacts

Cloudflare may set the strictly necessary security cookie __cf_bm (bot protection, expires within 30 minutes).
Map tiles (OpenFreeMap), fonts (Google Fonts) and flag icons (flagcdn.com) are loaded from those providers; they receive your IP address as part of the request but GisViz sets no cookies for them and shares no account data with them.
Links to private visuals carry a short-lived signed token in the address (not a cookie) that only lets your browser fetch that visual's data.


3. What we do not use

No Google Analytics or similar, no advertising or retargeting cookies, no social media pixels, no session recording, no cross-site tracking.


4. Your choices

Log out to remove the session items. You can clear site data for gisviz.com in your browser settings at any time; blocking localStorage prevents signing in.


5. Contact

info@gisviz.com\
""",
    },

    # ── ACCESSIBILITY ────────────────────────────────────────────────────────
    "accessibility": {
        "title": "Accessibility Statement",
        "last_updated": "July 2026",
        "content": """\
GisViz is committed to making the platform usable by as many people as possible, regardless of ability or the assistive technology they use.


Our target standard

We aim to conform to the Web Content Accessibility Guidelines (WCAG) 2.1 at Level AA. These guidelines cover a broad range of recommendations for making web content more accessible to people with visual, auditory, motor, cognitive, and speech-related disabilities.


What we have implemented

Keyboard navigation
The core feed, post detail pages, profile pages, authentication flows, and settings pages can be navigated using a keyboard alone, without a mouse.

Color contrast
Text and interactive elements are built using a design token system that has been reviewed for WCAG 2.1 AA contrast ratios.

Focus indicators
All interactive elements display a visible focus ring when focused via keyboard.

Semantic HTML
We use correct heading hierarchy (h1 → h2 → h3), ARIA landmark elements (main, nav, header, footer), and distinguish button from anchor elements appropriately.

Images
User-uploaded visualizations use the post title as alt text. Profile avatar images use the user's handle as alt text. Decorative images are marked with empty alt attributes.

Responsive design
The interface adapts to screen sizes from 320px (small mobile) upward.

Reduced motion
The platform respects the prefers-reduced-motion media query and limits or disables animations for users who have enabled that setting on their device.


Known limitations

Some sections of the admin panel have been tested for keyboard navigation but have not yet undergone full screen reader testing with multiple assistive technologies.

Complex geospatial visualizations uploaded by users are raster images. We strongly encourage publishers to use the post description field to describe the content of their visualization in plain text, but we cannot enforce this for every post.

The image quality validation tool that runs at upload time uses a canvas-based algorithm with visual feedback. A fully equivalent keyboard-only and screen-reader-accessible version of that feedback is on our roadmap.


Assistive technology tested

We have tested GisViz with:
  — Keyboard-only navigation in Chrome and Firefox on Windows and macOS.
  — VoiceOver with Safari on macOS.
  — NVDA with Chrome on Windows.

We have not yet completed formal testing with JAWS or TalkBack. Testing with additional assistive technologies is ongoing.


Reporting an accessibility issue

If you encounter any barrier that prevents you from using GisViz, we want to hear about it. Please email info@gisviz.com and include:
  — A description of the barrier you encountered.
  — The page or feature affected.
  — Your browser, operating system, and any assistive technology you are using.

We aim to acknowledge accessibility reports within 2 business days and to resolve confirmed issues as a priority.\
""",
    },

    # ── DATA, SHARING & LICENSING ───────────────────────────────────────────
    "data-policy": {
        "title": "Data, Sharing & Licensing",
        "last_updated": "October 2026",
        "content": """\
Effective date: October 4, 2026

This page explains how data moves through GisViz: who can see datasets and posts, how sharing and organisations work, how licences and attribution are applied, and how live data arrives.


1. Datasets: batch and stream

Batch datasets are files (CSV, Parquet, GeoJSON, …) loaded by our team or a dataset's owner and refreshed now and then when new data is available. Posts built on them are standard posts.
Stream datasets receive new rows continuously from the data owner's systems, through a secret ingest key. Posts built on them are live posts: the visual re-reads the data at the dataset's refresh interval and shows when it was last updated. A stream dataset can keep only its newest rows (retention).


2. Public and private

Every post and every dataset is public or private.
  — Public: anyone can see it; public posts appear in the feed, search, profiles and the sitemap.
  — Private: only the owner, GisViz administrators (and moderators, for posts), the people and organisations it is shared with, and the members of the organisation that owns it. Private posts never appear in feeds, search, the sitemap or other people's profiles; a link to a private post shows "not found" to everyone else.
Charts of private datasets are delivered through short-lived signed links, issued only to people allowed to see them.


3. The four combinations

  — Public post, public dataset: everyone sees the visual; the rows (Data tab) are shown when the dataset's licence allows it and its owner chose to publish them.
  — Public post, private dataset: everyone sees the visual, nobody but the dataset's managers sees the rows. Only someone who controls the dataset may publish such a post.
  — Private post, public dataset: only the people with access to the post see it.
  — Private post, private dataset: only people with access to the post see the visual; only people with access to the dataset see its rows.


4. Sharing and its record

Owners share a post or dataset with a person (@handle) or with an organisation (its @handle: every member gets access). Shared items appear in the Shared tab of each recipient's profile, with who shared them and when. Revoking access takes effect immediately. GisViz keeps the full history: who shared, with whom, when, and who revoked it and when; owners can see it in the access panel.


5. Organisations

An organisation account is registered with an email address on the organisation's own domain (public mail providers such as Gmail are not accepted) and reviewed by GisViz before it is verified. Every user whose verified email address is on a verified organisation's domain is a member and sees what is shared with the organisation and the organisation's own private content. Changing your email address changes your memberships accordingly.


6. Licences, copyright and attribution

Each dataset declares a licence category, the copyright holder and, where required, the attribution text shown with every visual built on it:
  — Open: CC0, ODC PDDL, public domain, CC BY 4.0, CC BY-SA 4.0, ODC-By, ODbL (e.g. OpenStreetMap), UK OGL v3.
  — Restricted: CC BY-NC 4.0 (non-commercial), "used with permission", other licences — readers see the visual; rows are not redistributed (except CC BY-NC, whose rows may be shown).
  — Closed: "all rights reserved" — private datasets only.
A public dataset must have a licence. Share-alike licences require derived data to keep the same licence. Rights holders can request removal at info@gisviz.com.


7. Personal data in datasets

Datasets that contain personal data are flagged; their rows are never shown to readers (only to the dataset's managers) and they should be private. Uploaders are responsible for having a lawful basis under the GDPR. People can ask us at info@gisviz.com to review or remove data about them.


8. Your data

Your own account data can be downloaded or deleted at any time from Settings → Privacy (see the Privacy Policy).


9. Contact

info@gisviz.com\
""",
    },
}


class LegalContent(BaseModel):
    title: str
    last_updated: str
    content: str


@router.get("/")
def list_legal_pages():
    return {"slugs": sorted(VALID_SLUGS)}


@router.get("/{slug}")
def get_legal_page(slug: str):
    if slug not in VALID_SLUGS:
        raise HTTPException(status_code=404, detail="Legal page not found")
    cached = cache_service.get(f"legal:v2:{slug}")
    if cached:
        return cached
    default = _DEFAULTS[slug]
    cache_service.set(f"legal:v2:{slug}", default, ttl_seconds=_TTL)
    return default


@router.put("/{slug}")
def update_legal_page(
    slug: str,
    payload: LegalContent,
    _: PlatformUserRecord = Depends(RoleChecker(["admin"])),
):
    if slug not in VALID_SLUGS:
        raise HTTPException(status_code=404, detail="Legal page not found")
    data = payload.model_dump()
    cache_service.set(f"legal:v2:{slug}", data, ttl_seconds=_TTL)
    return {"status": "updated", "slug": slug}