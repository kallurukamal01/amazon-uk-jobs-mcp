import asyncio
import json
import os
import re

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "Amazon UK Jobs",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "10000")),
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 AmazonUKJobsMCP/2.0"
)
AMAZON_JOBS_API = "https://www.amazon.jobs/en/search.json"
WAREHOUSE_SITE = "https://www.jobsatamazon.co.uk"
WAREHOUSE_SITEMAP = WAREHOUSE_SITE + "/sitemap.xml"

# County / region names -> amazon.jobs state codes and towns inside them.
# Lets "Kent" match jobs listed as "GB, KEN, Maidstone" or just "Dartford".
REGIONS = {
    "kent": {
        "codes": ["KEN"],
        "towns": [
            "maidstone", "gillingham", "chatham", "rochester", "medway",
            "strood", "dartford", "gravesend", "sittingbourne", "ashford",
            "canterbury", "tonbridge", "tunbridge wells", "sevenoaks",
            "swanley", "faversham", "folkestone", "dover", "margate",
            "ramsgate", "aylesford", "west malling", "kings hill",
        ],
    },
    "essex": {
        "codes": ["ESS"],
        "towns": [
            "basildon", "tilbury", "thurrock", "grays", "chelmsford",
            "colchester", "southend", "harlow", "brentwood", "purfleet",
        ],
    },
    "london": {
        "codes": ["LND"],
        "towns": [
            "london", "croydon", "enfield", "barking", "dagenham",
            "hayes", "feltham", "bexley", "bromley", "ilford",
        ],
    },
    "medway": {
        "codes": [],
        "towns": ["gillingham", "chatham", "rochester", "strood", "medway"],
    },
}

UK_WORDS = {"", "uk", "united kingdom", "gb", "great britain", "england"}


def _location_terms(location: str):
    """Return (state_codes, place_words) to match for a location query."""
    loc = (location or "").strip().lower()
    if loc in UK_WORDS:
        return None
    codes, words = set(), {loc}
    for name, info in REGIONS.items():
        if name in loc:
            codes.update(c.lower() for c in info["codes"])
            words.update(info["towns"])
    return codes, words


def _region_for(location: str):
    """County a small town belongs to, e.g. 'Gillingham' -> 'kent'."""
    loc = (location or "").strip().lower()
    for name, info in REGIONS.items():
        if name != "medway" and loc in info["towns"]:
            return name
    return None


def _matches(text_fields, state_code, terms) -> bool:
    if terms is None:
        return True
    codes, words = terms
    if state_code and state_code.lower() in codes:
        return True
    blob = " ".join(f for f in text_fields if f).lower().replace("-", " ")
    return any(w and w in blob for w in words)


def _job_locations(job):
    """Return every site of an amazon.jobs posting as a list of dicts."""
    sites = []
    for raw in job.get("locations") or []:
        try:
            loc = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            continue
        sites.append(loc)
    if not sites:
        sites.append({
            "city": job.get("city"),
            "region": job.get("state"),
            "location": job.get("location"),
            "normalizedLocation": job.get("normalized_location"),
        })
    return sites


def _matching_sites(job, terms):
    """Sites of a job that match the location query (all sites if no filter)."""
    sites = _job_locations(job)
    if terms is None:
        return sites
    return [
        s for s in sites
        if _matches(
            [s.get("city"), s.get("normalizedCityName"),
             s.get("normalizedCountyName"), s.get("location"),
             s.get("normalizedLocation")],
            s.get("region"),
            terms,
        )
    ]


def _site_label(site):
    city = site.get("normalizedCityName") or site.get("city") or "Unknown"
    county = site.get("normalizedCountyName")
    return f"{city}, {county}" if county and county != city else city


async def _fetch_page(client, keyword, offset, page_size):
    params = {
        "base_query": keyword,
        "country": "GBR",
        "offset": offset,
        "result_limit": page_size,
        "sort": "recent",
    }
    r = await client.get(AMAZON_JOBS_API, params=params)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def search_amazon_jobs(
    keyword: str = "",
    location: str = "United Kingdom",
    limit: int = 10,
) -> str:
    """Search Amazon UK salaried and corporate vacancies on amazon.jobs.

    Use this for office, engineering, management, operations-manager and
    technician roles. Filter by town, city or county (for example
    "Maidstone", "Kent", "London"). For hourly warehouse / shift work,
    use search_warehouse_shift_jobs instead.
    """
    limit = max(1, min(limit, 50))
    terms = _location_terms(location)
    page_size = 100

    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            first = await _fetch_page(client, keyword, 0, page_size)
            jobs = list(first.get("jobs", []))
            total = int(first.get("hits", 0) or 0)

            # Only page through everything when filtering by location,
            # because amazon.jobs ignores free-text locations.
            if terms is not None and total > page_size:
                offsets = range(page_size, min(total, 1000), page_size)
                pages = await asyncio.gather(
                    *(_fetch_page(client, keyword, o, page_size) for o in offsets),
                    return_exceptions=True,
                )
                for p in pages:
                    if isinstance(p, dict):
                        jobs.extend(p.get("jobs", []))

        def _filter(t):
            out = []
            for j in jobs:
                sites = _matching_sites(j, t)
                if sites:
                    out.append((j, sites))
            return out

        matched = _filter(terms)
        note = ""
        region = _region_for(location)
        if not matched and region:
            matched = _filter(_location_terms(region))
            if matched:
                note = (f"No jobs listed in {location} itself, so showing "
                        f"nearby jobs in {region.title()}.\n")

        if not matched:
            where = location or "the UK"
            return (
                f"No amazon.jobs vacancies found for '{keyword or 'any role'}' "
                f"in {where}. Try a nearby larger town, or use "
                "search_warehouse_shift_jobs for hourly warehouse work."
            )

        lines = [
            f"{note}Found {len(matched)} matching job(s); showing {min(limit, len(matched))}."
        ]
        for job, sites in matched[:limit]:
            path = job.get("job_path", "")
            link = "https://www.amazon.jobs" + path if path.startswith("/") else path
            where = "; ".join(dict.fromkeys(_site_label(x) for x in sites))
            lines.append(
                f"Job: {job.get('title', 'Title unavailable').strip()}\n"
                f"Location: {where}\n"
                f"Schedule: {job.get('job_schedule_type') or 'Not stated'}\n"
                f"Posted: {job.get('posted_date') or 'Not stated'}\n"
                f"Job ID: {job.get('id_icims', '')}\n"
                f"Apply: {link}"
            )
        return "\n---\n".join(lines)

    except Exception as error:
        return f"Amazon job search error: {error}"


@mcp.tool()
async def search_warehouse_shift_jobs(
    location: str = "",
    limit: int = 20,
) -> str:
    """Find hourly Amazon UK warehouse / shift jobs from jobsatamazon.co.uk.

    Reads the site's public job sitemap (warehouse associate, delivery
    station and similar hourly roles) and filters by town or county, for
    example "Kent", "Dartford" or "Southampton". Also returns the official
    shift search link, where live shifts, pay and start dates are shown.
    """
    limit = max(1, min(limit, 50))
    terms = _location_terms(location)
    search_link = WAREHOUSE_SITE + "/app#/jobSearch"

    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/xml"},
        ) as client:
            r = await client.get(WAREHOUSE_SITEMAP)
            r.raise_for_status()
            xml = r.text

        entries = re.findall(
            r"<loc>(https://www\.jobsatamazon\.co\.uk/jobDetail/[^<]+)</loc>"
            r"(?:\s*<lastmod>([^<]+)</lastmod>)?",
            xml,
        )
        jobs = []
        for url, lastmod in entries:
            parts = url.rstrip("/").split("/")
            # .../jobDetail/en-GB/<Title>/<Town>/<JobId>
            if len(parts) < 4:
                continue
            title = parts[-3].replace("-", " ")
            town = parts[-2].replace("-", " ")
            job_id = parts[-1]
            jobs.append((title, town, job_id, url, lastmod))

        matched = [j for j in jobs if _matches([j[1], j[0]], None, terms)]

        header = (
            "Hourly Amazon UK warehouse jobs (from jobsatamazon.co.uk).\n"
            "Live shifts, hourly pay and start dates are only shown on the "
            f"official search page: {search_link}"
        )

        if not matched:
            where = location or "the UK"
            listed = ", ".join(sorted({j[1] for j in jobs})) or "none"
            return (
                f"{header}\n\nNo listed warehouse jobs found in {where} right now.\n"
                f"Towns currently listed: {listed}.\n"
                "Open the search link above and enter your postcode to see "
                "every shift near you, or sign up for job alerts there."
            )

        lines = [f"{header}\nFound {len(matched)} job(s); showing {min(limit, len(matched))}."]
        for title, town, job_id, url, lastmod in matched[:limit]:
            lines.append(
                f"Job: {title}\n"
                f"Location: {town}\n"
                f"Job ID: {job_id}\n"
                f"Updated: {lastmod or 'Not stated'}\n"
                f"Apply: {url}"
            )
        return "\n---\n".join(lines)

    except Exception as error:
        return (
            f"Warehouse job search error: {error}\n"
            f"Search manually here: {search_link}"
        )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
