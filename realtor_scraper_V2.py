#!/usr/bin/env python3
"""
realtor_scraper.py
===================

Scrapes property listings from REALTOR.ca's own search API (the same
endpoint the website's JavaScript calls when you browse listings or change
pages) and saves the results as JSON.

HOW THIS WAS BUILT
-------------------
REALTOR.ca renders its listing pages client-side. The actual data comes
from a POST request to:

    https://api2.realtor.ca/Listing.svc/AsyncPropertySearch_Post

This was confirmed by watching the site's own network traffic while
paginating through https://www.realtor.ca/ab/calgary/real-estate. Because
this hits REALTOR.ca's internal API directly (instead of parsing rendered
HTML), it's much less fragile than a classic HTML scraper -- but it also
means it can break if REALTOR.ca changes that API without notice.

IMPORTANT LIMITS / THINGS TO KNOW
----------------------------------
1. REALTOR.ca caps how many listings you can page through for any single
   search/sort combination -- in testing, the API reported a hard cap of
   ~600 records ("MaxRecords") even though the Calgary search matched
   ~6,900+ listings total. This is a deliberate limit on their end, not a
   bug here. If you need more coverage, run this script multiple times
   with different filters (price bands, property type, etc. -- see
   `build_payload()`) and merge/dedupe the results by `mls_number`.
2. This calls a private/undocumented API that isn't meant for third-party
   use. REALTOR.ca's Terms of Use restrict automated scraping and
   redistributing MLS data commercially -- this script is intended for
   personal, non-commercial, rate-limited use (e.g. tracking listings
   you're personally interested in). Please review realtor.ca's Terms of
   Use yourself before relying on this, and don't hammer their servers --
   the default delay between requests is intentionally conservative.
3. REALTOR.ca may have bot-detection in front of this API. This script
   sends browser-like headers and warms up a session cookie first, which
   worked as of the time this was written, but if you start getting
   403/blocked responses, that's REALTOR.ca's anti-bot layer kicking in --
   slow down the --delay, or reduce request volume.

USAGE
-----
    python3 realtor_scraper.py
    python3 realtor_scraper.py --pages 20 --delay 2 --output calgary.json
    python3 realtor_scraper.py --geo-id g30_c3nfkdtg --records-per-page 20

Finding a GEO-ID for a different city/area:
    1. Open https://www.realtor.ca in a normal browser and search the
       area you want.
    2. Open DevTools -> Network tab, filter for "AsyncPropertySearch_Post".
    3. Look at the request's form body -- copy the "GeoIds" value.
    4. Pass it here with --geo-id.

Output is a single JSON file: a list of listing objects (see
`parse_listing()` for the exact fields), plus a small metadata block.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

import requests

SEARCH_URL = "https://api2.realtor.ca/Listing.svc/AsyncPropertySearch_Post"
WARMUP_URL = "https://www.realtor.ca/ab/calgary/real-estate"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-CA,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": WARMUP_URL,
    "Origin": "https://www.realtor.ca",
}


def build_payload(geo_id, page, records_per_page, transaction_type_id,
                   property_type_group_id, sort):
    """Build the form-encoded body REALTOR.ca's API expects.

    Field meanings, as observed from the live site (Calgary, "For Sale",
    residential, default "Newest" sort):
      GeoIds               -- area identifier (see module docstring)
      TransactionTypeId    -- 2 = For Sale on the page this was captured
                               from; REALTOR.ca uses a different value for
                               rentals. If you need rentals, browse
                               realtor.ca/.../rentals and re-capture this
                               value from the Network tab.
      PropertyTypeGroupID  -- 1 = Residential
      PropertySearchTypeId -- 1 (constant on the page this was captured)
      Sort                 -- "6-D" = Newest first (the page's default).
                               Other sort orders exist (price, etc.) but
                               weren't captured/verified here -- check the
                               Network tab if you want to add one.
    """
    return {
        "CurrentPage": page,
        "Sort": sort,
        "GeoIds": geo_id,
        "PropertyTypeGroupID": property_type_group_id,
        "TransactionTypeId": transaction_type_id,
        "PropertySearchTypeId": 1,
        "Currency": "CAD",
        "IncludeHiddenListings": "false",
        "RecordsPerPage": records_per_page,
        "ApplicationId": 1,
        "CultureId": 1,
        "Version": "7.0",
    }


def _num(text):
    """Best-effort: pull a number out of strings like '$685,000' or '1341 sqft'."""
    if not text:
        return None
    match = re.search(r"[\d,]+(\.\d+)?", str(text))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_listing(item):
    """Flatten one raw API listing object into a clean, JSON-friendly dict."""
    prop = item.get("Property") or {}
    building = item.get("Building") or {}
    address = prop.get("Address") or {}
    photos = prop.get("Photo") or []
    individuals = item.get("Individual") or []
    agent = individuals[0] if individuals else {}
    org = (agent.get("Organization") or {}) if agent else {}

    address_text = address.get("AddressText", "") or ""
    # AddressText looks like "365 Sunmills Drive SE|Calgary, Alberta T2X2T5"
    street, _, city_prov_postal = address_text.partition("|")

    price_raw = prop.get("Price")

    return {
        "mls_number": item.get("MlsNumber"),
        "listing_url": (
            "https://www.realtor.ca" + item["RelativeDetailsURL"]
            if item.get("RelativeDetailsURL") else None
        ),
        "price_raw": price_raw,
        "price": _num(price_raw),
        "property_type": prop.get("Type"),
        "address": street.strip(),
        "city_province_postal": city_prov_postal.strip(),
        "latitude": address.get("Latitude"),
        "longitude": address.get("Longitude"),
        "bedrooms": building.get("Bedrooms"),
        "bathrooms": building.get("BathroomTotal"),
        "half_bathrooms": building.get("HalfBathTotal"),
        "size_interior": building.get("SizeInterior"),
        "stories": building.get("StoriesTotal"),
        "building_type": building.get("Type"),
        "parking_spaces_total": prop.get("ParkingSpaceTotal"),
        "description": item.get("PublicRemarks"),
        "photo_url": photos[0].get("MedResPath") if photos else None,
        "listed_date_utc": item.get("InsertedDateUTC"),
        "days_on_market": item.get("TimeOnRealtor"),
        "agent_name": agent.get("Name"),
        "brokerage_name": org.get("Name"),
        "brokerage_phone": (
            org.get("Phones", [{}])[0].get("PhoneNumber")
            if org.get("Phones") else None
        ),
    }


def scrape(geo_id, max_pages, records_per_page, delay, transaction_type_id,
           property_type_group_id, sort):
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    # Warm up: fetch the human-facing page first so the session picks up
    # any cookies REALTOR.ca sets before we start hitting the API.
    try:
        session.get(WARMUP_URL, timeout=20)
    except requests.RequestException as exc:
        print(f"Warning: warm-up request failed ({exc}); continuing anyway.",
              file=sys.stderr)

    all_listings = []
    seen_mls = set()
    total_records = None
    max_records_cap = None

    page = 1
    while max_pages is None or page <= max_pages:
        payload = build_payload(
            geo_id, page, records_per_page, transaction_type_id,
            property_type_group_id, sort,
        )

        for attempt in range(3):
            try:
                resp = session.post(SEARCH_URL, data=payload, timeout=20)
                break
            except requests.RequestException as exc:
                print(f"Page {page}: request error ({exc}), "
                      f"retry {attempt + 1}/3...", file=sys.stderr)
                time.sleep(2 * (attempt + 1))
        else:
            print(f"Page {page}: giving up after 3 failed attempts.",
                  file=sys.stderr)
            break

        if resp.status_code != 200:
            print(f"Page {page}: HTTP {resp.status_code} -- stopping. "
                  f"(REALTOR.ca may be rate-limiting/blocking this request.)",
                  file=sys.stderr)
            break

        try:
            data = resp.json()
        except ValueError:
            print(f"Page {page}: response wasn't valid JSON -- stopping.",
                  file=sys.stderr)
            break

        if data.get("ErrorCode") not in (None, "200", 200):
            print(f"Page {page}: API returned ErrorCode={data.get('ErrorCode')} "
                  f"-- stopping.", file=sys.stderr)
            break

        paging = data.get("Paging") or {}
        total_records = paging.get("TotalRecords", total_records)
        max_records_cap = paging.get("MaxRecords", max_records_cap)

        results = data.get("Results") or []
        if not results:
            print(f"Page {page}: no more results.", file=sys.stderr)
            break

        new_count = 0
        for raw in results:
            parsed = parse_listing(raw)
            mls = parsed.get("mls_number")
            if mls and mls in seen_mls:
                continue
            if mls:
                seen_mls.add(mls)
            all_listings.append(parsed)
            new_count += 1

        print(f"Page {page}: got {len(results)} listings "
              f"({new_count} new, {len(all_listings)} total so far).",
              file=sys.stderr)

        records_showing = paging.get("RecordsShowing")
        if max_records_cap and records_showing and records_showing >= max_records_cap:
            print("Reached REALTOR.ca's MaxRecords cap for this search/sort "
                  "-- stopping. See the module docstring for how to get more.",
                  file=sys.stderr)
            break

        total_pages = paging.get("TotalPages")
        if total_pages and page >= total_pages:
            break

        page += 1
        time.sleep(delay)

    return {
        "listings": all_listings,
        "meta": {
            "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": WARMUP_URL,
            "geo_id": geo_id,
            "sort": sort,
            "listings_scraped": len(all_listings),
            "total_records_matching_search": total_records,
            "realtor_ca_page_cap": max_records_cap,
        },
    }


def main():
    parser = argparse.ArgumentParser(
        description="Scrape REALTOR.ca listings into a JSON file.")
    parser.add_argument("--geo-id", default="g30_c3nfkdtg",
                         help="REALTOR.ca GeoIds value (default: Calgary, AB).")
    parser.add_argument("--pages", type=int, default=None,
                         help="Max number of pages to fetch (default: until "
                              "REALTOR.ca's cap or results run out).")
    parser.add_argument("--records-per-page", type=int, default=50,
                         help="Listings per request (default: 50).")
    parser.add_argument("--delay", type=float, default=1.5,
                         help="Seconds to wait between requests (default: 1.5).")
    parser.add_argument("--transaction-type-id", type=int, default=2,
                         help="2 = For Sale, as captured from the site "
                              "(default: 2).")
    parser.add_argument("--property-type-group-id", type=int, default=1,
                         help="1 = Residential (default: 1).")
    parser.add_argument("--sort", default="6-D",
                         help="Sort order code (default: '6-D' = Newest).")
    parser.add_argument("--output", default="realtor_listings.json",
                         help="Output JSON file path.")

    # parse_known_args() (instead of parse_args()) so this still works when
    # run inside Jupyter: Jupyter launches the kernel as
    # "ipykernel_launcher.py -f <connection-file>.json", and that "-f ..."
    # ends up in sys.argv. parse_args() would choke on it as an unrecognized
    # argument ("unrecognized arguments: -f ...json"); parse_known_args()
    # just ignores anything it doesn't recognize and falls back to the
    # defaults above. Works identically from a normal command line too.
    args, _unknown_args = parser.parse_known_args()

    result = scrape(
        geo_id=args.geo_id,
        max_pages=args.pages,
        records_per_page=args.records_per_page,
        delay=args.delay,
        transaction_type_id=args.transaction_type_id,
        property_type_group_id=args.property_type_group_id,
        sort=args.sort,
    )

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\nSaved {len(result['listings'])} listings to {args.output}")
    print(f"(REALTOR.ca reported {result['meta']['total_records_matching_search']} "
          f"total matching listings; it caps browsable results at "
          f"{result['meta']['realtor_ca_page_cap']} per search/sort.)")


if __name__ == "__main__":
    main()
