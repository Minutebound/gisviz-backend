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

VALID_SLUGS = {"about", "privacy", "terms", "cookies", "accessibility"}
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
        "last_updated": "July 2026",
        "content": """\
Effective date: July 1, 2026

GisViz ("we," "us," or "our") operates gisviz.com. This Privacy Policy explains what personal information we collect, how we use it, and your rights regarding that information. By using GisViz, you agree to the practices described below.


1. Information We Collect

Account Information
When you register, we collect your email address, a username (handle), and a password (stored as a one-way bcrypt hash). We use this to authenticate you and send transactional emails such as verification codes and password reset links. We do not use your email address for marketing without your explicit consent.

Profile Information
You may optionally add a display title, LinkedIn URL, Medium URL, personal website URL, and a general location (city, state, country). This information is public and displayed on your profile page. You control what you share.

Published Content
Posts you create — titles, descriptions, source credits, category tags, keywords, and uploaded images — are public. Images are stored on our servers. Metadata is stored in our database.

Interaction Data
Likes, bookmarks, comments, and follow relationships are stored to power the platform's social features. These are linked to your user account.

Server Logs
Our servers record standard HTTP request logs (IP address, URL path, timestamp, response code) for security and debugging purposes. These logs are retained for 30 days and are not used for profiling or advertising.


2. What We Do Not Collect

We do not collect payment information — GisViz is currently free to use.
We do not sell, rent, or share your personal data with third parties for commercial or advertising purposes.
We do not run behavioral advertising or retargeting campaigns.
We do not use fingerprinting or cross-site tracking technologies.
We do not use third-party analytics services that receive your personal data.


3. How We Use Your Information

To operate your account and authenticate your sessions.
To send transactional emails you have requested (account verification, password reset).
To display your public profile and published content to other users.
To detect and prevent abuse, spam, and unauthorized access.
To improve the platform based on aggregated, non-identifiable usage patterns.


4. How Your Data Is Stored and Protected

All data is stored on servers we control. Passwords are hashed using bcrypt and are never stored or transmitted in plain text. Connections to our platform are encrypted via TLS. We take reasonable technical and organizational measures to protect your data against unauthorized access, loss, or disclosure.

We retain your account data for as long as your account is active. If you delete your account, we remove your profile, posts, comments, likes, and bookmarks. Your username may remain visible in other users' comment threads as "deleted user."


5. Your Rights

Depending on where you are located, you may have rights regarding your personal data, including the right to access, correct, or delete it. Regardless of your location, you can:

Access your data — review your profile and posts at any time while logged in.
Correct your data — update your profile via Settings.
Delete your account — go to Settings → Account → Delete Account. This permanently removes your account and associated content.
Request a data export — email us at info@gisviz.com.

To exercise any of these rights, contact us at info@gisviz.com. We will respond within 30 days.


6. California Residents (CCPA)

If you are a California resident, you have the right to know what personal information we collect, the right to delete your personal information, and the right to opt out of the sale of personal information. We do not sell personal information. To make a request under the CCPA, contact us at info@gisviz.com.


7. Children's Privacy

GisViz is not directed at children under the age of 13. We do not knowingly collect personal information from children under 13. If you believe a child under 13 has created an account, please contact us at info@gisviz.com and we will promptly delete the account.


8. Third-Party Services

We use a limited number of third-party services to operate the platform. These services may process data on our behalf:

Cloudflare — provides DNS, content delivery, and DDoS protection. Cloudflare may process IP addresses as part of routing and security operations. Privacy policy: cloudflare.com/privacypolicy.
IONOS — provides transactional email delivery for verification and password reset emails only.

We do not use any other third-party services that receive your personal data.


9. Changes to This Policy

We will notify registered users of material changes to this Privacy Policy by email before they take effect. The effective date at the top of this page reflects the current version. Continued use of GisViz after changes take effect constitutes acceptance of the updated policy.


10. Contact

GisViz
info@gisviz.com\
""",
    },

    # ── TERMS ────────────────────────────────────────────────────────────────
    "terms": {
        "title": "Terms of Service",
        "last_updated": "July 2026",
        "content": """\
Effective date: July 1, 2026

These Terms of Service ("Terms") govern your access to and use of GisViz ("we," "us," or "our") at gisviz.com (the "Service"). By creating an account or using the Service, you agree to be bound by these Terms. If you do not agree, do not use the Service.


1. The Service

GisViz is a platform for publishing and discovering geospatial visualizations. Users can create accounts, upload map imagery and spatial renders, write descriptions, tag posts by category and keyword, and interact with other users' content through likes, bookmarks, comments, and follows.


2. Eligibility

You must be at least 13 years old to use GisViz. If you are between 13 and 18, you represent that your parent or legal guardian has reviewed and agreed to these Terms on your behalf.

By using GisViz, you represent that all information you provide is accurate and that you have the legal capacity to enter into these Terms.


3. Your Account

You are responsible for maintaining the confidentiality of your login credentials. You may not share your account with others or create multiple accounts. Your username (handle) must not impersonate another person or organization.

You are responsible for all activity that occurs under your account. Notify us immediately at info@gisviz.com if you suspect unauthorized access.


4. Content You Publish

You retain ownership of the geospatial visualizations and descriptions you publish on GisViz.

By publishing content on GisViz, you grant us a non-exclusive, worldwide, royalty-free license to store, display, and distribute your content solely for the purpose of operating and improving the Service.

You represent and warrant that:
  — You have the right to publish the content (you created it, own the necessary data license, or have appropriate permission from the rights holder).
  — Your content does not infringe the intellectual property rights, privacy rights, or other rights of any third party.
  — Your content does not include sensitive, classified, or restricted geospatial data that you are not authorized to share publicly.
  — Uploaded imagery is your own original render or visualization. You may not upload screenshots of proprietary commercial mapping products (such as Google Maps, Mapbox, or Esri basemaps) without explicit written permission from the respective provider.

Data source attribution is required. All posts must include the Source Name field crediting the origin of the underlying data. Posts without proper attribution may be removed.


5. Prohibited Conduct

You agree not to use GisViz to:
  — Publish content you do not have rights to share.
  — Harass, threaten, impersonate, or intimidate other users.
  — Upload malicious code, viruses, or any software designed to damage or interfere with systems.
  — Submit spam, unsolicited commercial content, or automated posts without prior written permission.
  — Attempt to gain unauthorized access to the platform, other user accounts, or our servers.
  — Scrape or systematically download content without prior written agreement.
  — Use the Service for any unlawful purpose or in violation of any applicable federal, state, or local law.


6. Content Moderation

We reserve the right to remove content, suspend accounts, or terminate access to the Service for violations of these Terms, without prior notice in cases involving serious harm or illegal activity.

You may report content that violates these Terms using the Report button on any post. We review all reports and aim to respond within 5 business days.

Moderation decisions may be appealed by contacting info@gisviz.com.


7. Intellectual Property

The GisViz name, logo, design, and platform code are owned by GisViz and protected by applicable intellectual property laws. You may not use our trademarks or branding without prior written permission.


8. Disclaimers

THE SERVICE IS PROVIDED "AS IS" AND "AS AVAILABLE" WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, OR NON-INFRINGEMENT.

We do not warrant that the Service will be uninterrupted, error-free, or free of viruses. We do not guarantee the accuracy or completeness of any user-generated content, including geospatial data and visualizations.


9. Limitation of Liability

TO THE FULLEST EXTENT PERMITTED BY APPLICABLE LAW, GISVIZ SHALL NOT BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES ARISING FROM YOUR USE OF OR INABILITY TO USE THE SERVICE, EVEN IF WE HAVE BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGES.

OUR TOTAL LIABILITY TO YOU FOR ANY CLAIM ARISING FROM OR RELATED TO THESE TERMS OR THE SERVICE SHALL NOT EXCEED ONE HUNDRED DOLLARS ($100).

NOTHING IN THESE TERMS LIMITS LIABILITY FOR FRAUD, GROSS NEGLIGENCE, OR WILLFUL MISCONDUCT.


10. Governing Law and Dispute Resolution

These Terms are governed by the laws of the State of Delaware, United States, without regard to its conflict-of-law provisions.

Any dispute arising from or relating to these Terms or the Service will be resolved through binding arbitration administered by the American Arbitration Association (AAA) under its Consumer Arbitration Rules, except that either party may seek injunctive or other equitable relief in any court of competent jurisdiction. You waive any right to a jury trial or to participate in a class action.


11. Changes to These Terms

We will notify registered users of material changes to these Terms by email at least 14 days before they take effect. Continued use of the Service after that date constitutes acceptance of the updated Terms.


12. Contact

GisViz
info@gisviz.com\
""",
    },

    # ── COOKIES ──────────────────────────────────────────────────────────────
    "cookies": {
        "title": "Cookie Policy",
        "last_updated": "July 2026",
        "content": """\
Effective date: July 1, 2026

This Cookie Policy explains how GisViz uses cookies and similar browser storage technologies when you use gisviz.com.


1. What is a cookie?

A cookie is a small text file placed on your device by a website. We use this term broadly to include both traditional cookies and browser localStorage, since GisViz relies primarily on localStorage for session management.


2. What we use

Authentication Token (localStorage)
Key: gisviz_token
Purpose: Stores your JSON Web Token (JWT) to keep you logged in between page loads and sessions.
Duration: Until you log out or the token expires (typically 60 minutes of inactivity, configurable by us).
Type: First-party. Strictly necessary for using the platform.

User Handle (localStorage)
Key: gisviz_handle
Purpose: Stores your username so the interface can display it without an additional server request.
Duration: Until you log out.
Type: First-party. Strictly necessary for a functional logged-in experience.

These are the only items GisViz itself writes to your browser's storage.


3. What we do not use

We do not use:
  — Analytics cookies or localStorage (no Google Analytics, Mixpanel, Amplitude, or similar services).
  — Advertising or retargeting cookies.
  — Social media tracking pixels (no Meta Pixel, Twitter/X Pixel, TikTok Pixel, or similar).
  — Session recording tools (no Hotjar, FullStory, or similar).
  — Cross-site tracking of any kind.


4. Third-party cookies

Cloudflare, our content delivery and security provider, may set a short-lived browser cookie named __cf_bm for bot detection and DDoS protection. This cookie:
  — Is set by Cloudflare, not by GisViz directly.
  — Expires within 30 minutes.
  — Is strictly necessary for security and cannot be disabled without potentially blocking your access to the site.
  — Does not track you across websites.

For more information, see Cloudflare's privacy policy at cloudflare.com/privacypolicy.


5. Your choices

You can remove your authentication token at any time by clicking Log out in the navigation. This removes both localStorage items listed above.

You can also clear site data manually through your browser:
  Chrome / Edge: Settings → Privacy and security → Site settings → View permissions and data stored across sites → gisviz.com → Clear data.
  Firefox: Preferences → Privacy & Security → Cookies and Site Data → Manage Data → search for gisviz.com → Remove Selected.
  Safari: Preferences → Privacy → Manage Website Data → search for gisviz.com → Remove.

Note: Clearing your authentication token will log you out. Blocking localStorage entirely will prevent you from logging in to GisViz.


6. Changes to this policy

If we introduce new cookies or storage items in the future, we will update this policy and notify registered users before the change takes effect.


7. Contact

Questions about this policy or our use of cookies:
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
    cached = cache_service.get(f"legal:{slug}")
    if cached:
        return cached
    default = _DEFAULTS[slug]
    cache_service.set(f"legal:{slug}", default, ttl_seconds=_TTL)
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
    cache_service.set(f"legal:{slug}", data, ttl_seconds=_TTL)
    return {"status": "updated", "slug": slug}