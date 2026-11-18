#!/usr/bin/env python3
"""Mirror external IEM recommendation images into the existing Supabase Storage bucket.

Requires: pip install requests pillow beautifulsoup4
Environment: SUPABASE_SERVICE_ROLE_KEY (never commit this secret)
Usage:
    python scripts/mirror_recommendation_images.py
    python scripts/mirror_recommendation_images.py --apply

Without --apply, prints a dry run. With --apply, uploads images and rewrites
the article locally. Review the diff, then commit the updated HTML.
"""
import argparse
import hashlib
import io
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

BASE = "https://beeowbqgrlcnlfoocmvi.supabase.co"
BUCKET = "iem-product-images"
ARTICLE = Path(__file__).resolve().parents[1] / "precog-iem-recommendations-by-price.html"
MAX_SOURCE_BYTES = 12 * 1024 * 1024
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
TIMEOUT = (10, 25)


def get_image(url: str) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Only HTTPS image URLs are supported")
    response = requests.get(url, timeout=TIMEOUT, stream=True, headers={
        "User-Agent": "Mozilla/5.0 (compatible; IEMMarketTracker/1.0; image-import)"
    })
    response.raise_for_status()
    content = bytearray()
    for chunk in response.iter_content(64 * 1024):
        content.extend(chunk)
        if len(content) > MAX_SOURCE_BYTES:
            raise ValueError("Source image exceeds 12MB limit")
    try:
        with Image.open(io.BytesIO(content)) as im:
            im.load()
            if im.width < 160 or im.height < 160:
                raise ValueError("Source image too small")
            if im.width * im.height > 24_000_000:
                raise ValueError("Source image dimensions too large")
            image = im.convert("RGB")
            output = io.BytesIO()
            image.save(output, "WEBP", quality=85, method=5)
            result = output.getvalue()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError("Invalid image") from e
    if len(result) > MAX_UPLOAD_BYTES:
        raise ValueError("Converted image exceeds bucket's 5MB limit")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Upload and rewrite HTML")
    args = parser.parse_args()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if args.apply and not key:
        parser.error("SUPABASE_SERVICE_ROLE_KEY is required for --apply")
    soup = BeautifulSoup(ARTICLE.read_text(encoding="utf-8"), "html.parser")
    prefix = BASE + "/storage/v1/object/public/" + BUCKET + "/"
    updated, failed = 0, []
    for card in soup.select("article.iem-card"):
        img = card.select_one("img.iem-image")
        if not img:
            continue
        source = img.get("src", "")
        if source.startswith(prefix):
            continue
        title = card.select_one("h3")
        name = title.get_text(" ", strip=True) if title else "unknown"
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        short_hash = hashlib.sha256(source.encode()).hexdigest()[:10]
        obj = f"recommendations/{slug}-{short_hash}.webp"
        target = prefix + obj
        if not args.apply:
            print(f"WOULD MIRROR: {name} -> {obj}")
            updated += 1
            continue
        try:
            payload = get_image(source)
            response = requests.post(
                f"{BASE}/storage/v1/object/{BUCKET}/{obj}",
                headers={
                    "Authorization": f"Bearer {key}",
                    "apikey": key,
                    "Content-Type": "image/webp",
                    "Cache-Control": "3600",
                    "x-upsert": "false",
                },
                data=payload,
                timeout=TIMEOUT,
            )
            if response.status_code == 409:  # already exists; reuse stable name
                pass
            else:
                response.raise_for_status()
            img["src"] = target
            img.attrs.pop("referrerpolicy", None)
            updated += 1
            print(f"MIRRORED: {name} -> {obj}")
        except Exception as exc:
            failed.append((name, str(exc)))
            print(f"FAILED: {name}: {exc}")
    if args.apply and updated:
        ARTICLE.write_text(str(soup), encoding="utf-8")
    print(f"Image references {'planned' if not args.apply else 'updated'}: {updated}; failures: {len(failed)}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
