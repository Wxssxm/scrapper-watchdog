"""
Example scraper compatible with scraper-watchdog.

This scraper fetches the Hacker News front page and writes the top stories
(title, score, url) to the CSV path provided via OUTPUT_PATH.

Run manually:
    OUTPUT_PATH=data/hn.csv python scrapers/example_scraper.py

With watchdog (after setting up configs/example.yaml):
    scraper-watchdog --config configs/example.yaml --source hn_top_stories
"""
import csv
import os
import time
import random

import httpx
from bs4 import BeautifulSoup

OUTPUT_PATH = os.environ["OUTPUT_PATH"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

url = "https://news.ycombinator.com/"

time.sleep(random.uniform(1, 3))

with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30) as client:
    response = client.get(url)
    response.raise_for_status()

soup = BeautifulSoup(response.text, "lxml")

rows = []
for item in soup.select("tr.athing"):
    title_el = item.select_one(".titleline > a")
    subtext = item.find_next_sibling("tr")
    score_el = subtext.select_one(".score") if subtext else None

    if not title_el:
        continue

    rows.append({
        "title": title_el.get_text(strip=True),
        "url": title_el.get("href", ""),
        "score": score_el.get_text(strip=True).replace(" points", "") if score_el else "0",
    })

if not rows:
    raise ValueError("No stories found — page structure may have changed")

os.makedirs(os.path.dirname(os.path.abspath(OUTPUT_PATH)), exist_ok=True)
with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=["title", "url", "score"])
    writer.writeheader()
    writer.writerows(rows)

print(f"Scraped {len(rows)} stories → {OUTPUT_PATH}")
