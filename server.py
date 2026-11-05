import os
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "Amazon UK Jobs",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "10000"))
)

@mcp.tool()
async def search_amazon_jobs(
    keyword: str = "warehouse",
    location: str = "United Kingdom",
    limit: int = 10
) -> str:
    """Search public Amazon UK job vacancies."""

    limit = max(1, min(limit, 50))

    url = "https://www.amazon.jobs/en/search.json"

    params = {
        "base_query": keyword,
        "loc_query": location,
        "country": "GBR",
        "offset": 0,
        "result_limit": limit,
        "sort": "recent"
    }

    headers = {
        "User-Agent": "Mozilla/5.0 AmazonUKJobsMCP/1.0"
    }

    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True
        ) as client:
            response = await client.get(
                url,
                params=params,
                headers=headers
            )
            response.raise_for_status()
            data = response.json()

        jobs = data.get("jobs", [])

        if not jobs:
            return "No matching jobs found. Try another keyword or location."

        results = []

        for job in jobs:
            title = job.get("title", "Job title unavailable")
            place = job.get(
                "location",
                job.get("normalized_location", "Location unavailable")
            )
            job_id = job.get("id_icims", "")
            path = job.get("job_path", "")
            link = (
                "https://www.amazon.jobs" + path
                if path.startswith("/")
                else path
            )

            results.append(
                f"Job: {title}\n"
                f"Location: {place}\n"
                f"Job ID: {job_id}\n"
                f"Apply: {link}\n"
            )

        return "\n---\n".join(results)

    except Exception as error:
        return f"Amazon job search error: {error}"

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
