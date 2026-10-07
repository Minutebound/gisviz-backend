"""
app/services/licences.py
========================
Copyright / licence categories a dataset can carry (datasets.license_code). They decide what GisViz may do
with the data, shown on the dataset and on every post built on it:

  attribution    the attribution text (datasets.attribution) must be shown with every visual
  rows_public    the rows may be shown to readers (Data tab) — otherwise only the visual (aggregates) is shown
  commercial     commercial re-use allowed (shown to readers; GisViz itself shows ads)
  share_alike    derived data must use the same licence (shown to readers)
  public_ok      the dataset may be PUBLIC on GisViz (all rights reserved data may only be private)

Unknown / empty codes (datasets created before licensing existed) behave like before: rows follow the
dataset's show_data switch. New public datasets must pick a licence.
"""
from __future__ import annotations

from typing import Optional

LICENCES: list[dict] = [
    {"code": "cc0-1.0", "name": "CC0 1.0 (public domain dedication)", "group": "open",
     "url": "https://creativecommons.org/publicdomain/zero/1.0/",
     "attribution": False, "rows_public": True, "commercial": True, "share_alike": False, "public_ok": True},
    {"code": "pddl-1.0", "name": "ODC Public Domain Dedication and Licence (PDDL)", "group": "open",
     "url": "https://opendatacommons.org/licenses/pddl/1-0/",
     "attribution": False, "rows_public": True, "commercial": True, "share_alike": False, "public_ok": True},
    {"code": "public-domain", "name": "Public domain (e.g. US federal government works)", "group": "open",
     "url": None,
     "attribution": False, "rows_public": True, "commercial": True, "share_alike": False, "public_ok": True},
    {"code": "cc-by-4.0", "name": "CC BY 4.0 (attribution)", "group": "open",
     "url": "https://creativecommons.org/licenses/by/4.0/",
     "attribution": True, "rows_public": True, "commercial": True, "share_alike": False, "public_ok": True},
    {"code": "cc-by-sa-4.0", "name": "CC BY-SA 4.0 (attribution, share-alike)", "group": "open",
     "url": "https://creativecommons.org/licenses/by-sa/4.0/",
     "attribution": True, "rows_public": True, "commercial": True, "share_alike": True, "public_ok": True},
    {"code": "odc-by-1.0", "name": "ODC Attribution Licence (ODC-By)", "group": "open",
     "url": "https://opendatacommons.org/licenses/by/1-0/",
     "attribution": True, "rows_public": True, "commercial": True, "share_alike": False, "public_ok": True},
    {"code": "odbl-1.0", "name": "ODC Open Database Licence (ODbL), e.g. OpenStreetMap", "group": "open",
     "url": "https://opendatacommons.org/licenses/odbl/1-0/",
     "attribution": True, "rows_public": True, "commercial": True, "share_alike": True, "public_ok": True},
    {"code": "ogl-uk-3.0", "name": "Open Government Licence v3.0 (UK)", "group": "open",
     "url": "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
     "attribution": True, "rows_public": True, "commercial": True, "share_alike": False, "public_ok": True},
    {"code": "cc-by-nc-4.0", "name": "CC BY-NC 4.0 (attribution, non-commercial)", "group": "restricted",
     "url": "https://creativecommons.org/licenses/by-nc/4.0/",
     "attribution": True, "rows_public": True, "commercial": False, "share_alike": False, "public_ok": True},
    {"code": "permission", "name": "Used with the copyright holder's permission", "group": "restricted",
     "url": None,
     "attribution": True, "rows_public": False, "commercial": False, "share_alike": False, "public_ok": True},
    {"code": "other", "name": "Other licence (see the source)", "group": "restricted",
     "url": None,
     "attribution": True, "rows_public": False, "commercial": False, "share_alike": False, "public_ok": True},
    {"code": "all-rights-reserved", "name": "All rights reserved (private use only)", "group": "closed",
     "url": None,
     "attribution": True, "rows_public": False, "commercial": False, "share_alike": False, "public_ok": False},
]
BY_CODE = {l["code"]: l for l in LICENCES}


def get(code: Optional[str]) -> Optional[dict]:
    return BY_CODE.get((code or "").strip().lower()) if code else None


def valid(code: Optional[str]) -> bool:
    return not code or (code or "").strip().lower() in BY_CODE


def rows_public(code: Optional[str]) -> bool:
    """May readers see the rows? Unknown (legacy) codes keep the old behaviour (True: show_data decides)."""
    lic = get(code)
    return True if lic is None else bool(lic["rows_public"])


def public_ok(code: Optional[str]) -> bool:
    lic = get(code)
    return True if lic is None else bool(lic["public_ok"])


def needs_attribution(code: Optional[str]) -> bool:
    lic = get(code)
    return bool(lic and lic["attribution"])


def summary(code: Optional[str]) -> Optional[dict]:
    """What a reader is told about the licence (post page, dataset card)."""
    lic = get(code)
    if lic is None:
        return None
    return {k: lic[k] for k in ("code", "name", "url", "attribution", "commercial", "share_alike", "rows_public", "group")}