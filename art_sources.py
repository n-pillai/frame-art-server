"""
art_sources.py — Fetch public domain art from museum APIs and local folders.

Supports:
  - The Metropolitan Museum of Art (no API key needed)
  - Rijksmuseum (no API key needed — new Search API)
  - Art Institute of Chicago (no API key needed)
  - Cleveland Museum of Art (no API key needed)
  - Wikimedia Commons (no API key needed)
  - Local image folders
"""

import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("frame_art.sources")

# ---------------------------------------------------------------------------
# Landscape aspect ratio filter
# ---------------------------------------------------------------------------
# Samsung Frame TV is 16:9 (1.778). We only accept images that are landscape
# enough that a gentle center-crop to 16:9 won't ruin the composition.
# min_aspect = 1.3 means we accept anything from ~4:3 and wider.
# This excludes portraits, squares, and near-square images entirely.
MIN_ASPECT_RATIO = 1.3  # width / height must be >= this


def is_landscape_enough(width, height, min_aspect: float = MIN_ASPECT_RATIO) -> bool:
    """Return True if the image is wide enough for a 16:9 TV without heavy cropping."""
    try:
        w, h = float(width), float(height)
    except (TypeError, ValueError):
        return False
    if h == 0:
        return False
    return (w / h) >= min_aspect


# ---------------------------------------------------------------------------
# Notable artist filter — first-rotation "greatest hits" mode
# ---------------------------------------------------------------------------
# When major_artists_only=True, only artworks by these artists (or matching
# these keywords in the artist field) are accepted. This keeps the first USB
# batch full of recognizable masterpieces.

MAJOR_ARTISTS = {
    # Impressionism & Post-Impressionism (expanded)
    "monet", "renoir", "degas", "cézanne", "cezanne", "pissarro", "sisley",
    "manet", "berthe morisot", "mary cassatt", "gustave caillebotte",
    "van gogh", "gauguin", "seurat", "signac", "toulouse-lautrec",
    "bazille", "frédéric bazille", "armand guillaumin", "guillaumin",
    "childe hassam", "hassam", "maximilien luce",
    # Dutch & Flemish Masters
    "rembrandt", "vermeer", "jan steen", "jacob van ruisdael", "ruisdael",
    "pieter bruegel", "brueghel", "rubens", "van dyck", "frans hals",
    "meindert hobbema", "aelbert cuyp",
    # Renaissance & Baroque
    "leonardo", "raphael", "michelangelo", "titian", "tintoretto",
    "caravaggio", "velázquez", "velasquez", "el greco", "botticelli",
    "giorgione", "veronese", "poussin", "claude lorrain",
    # Romanticism & Realism
    "turner", "constable", "delacroix", "géricault", "gericault",
    "courbet", "corot", "millet", "daubigny", "rosa bonheur",
    "caspar david friedrich", "frederic edwin church", "thomas cole",
    "albert bierstadt", "winslow homer", "thomas moran",
    # Landscape & nature specialists
    "isaac levitan", "levitan", "arkhip kuindzhi", "kuindzhi",
    "ivan shishkin", "shishkin", "john martin", "samuel palmer",
    "théodore rousseau", "charles-françois daubigny",
    # Maritime & seascapes
    "ivan aivazovsky", "aivazovsky",
    "willem van de velde", "van de velde",
    "ludolf bakhuizen", "bakhuizen",
    "andreas achenbach", "achenbach",
    # British art & Pre-Raphaelites
    "gainsborough", "reynolds", "stubbs", "george stubbs",
    "john everett millais", "millais",
    "john william waterhouse", "waterhouse",
    "edward burne-jones", "burne-jones",
    "dante gabriel rossetti", "rossetti",
    "ford madox brown", "lord leighton", "frederic leighton",
    "john atkinson grimshaw", "atkinson grimshaw",
    # Japanese
    "hokusai", "hiroshige", "utamaro",
    # Modern (pre-war)
    "klimt", "schiele", "kandinsky", "mondrian", "matisse", "picasso",
    "chagall", "dalí", "dali", "munch", "bonnard", "vuillard",
    "georgia o'keeffe", "o'keeffe", "edward hopper", "hopper",
    "paul klee", "klee", "malevich",
    # Abstract Expressionism
    "rothko", "mark rothko", "pollock", "jackson pollock",
    "de kooning", "willem de kooning",
    "helen frankenthaler", "frankenthaler",
    "joan mitchell",
    # Indian — classic & modern
    "raja ravi varma", "ravi varma", "amrita sher-gil", "abanindranath tagore",
    "nandalal bose",
    "m.f. husain", "maqbool fida husain", "husain",
    "s.h. raza", "sayed haider raza", "raza",
    "f.n. souza", "francis newton souza", "souza",
    "tyeb mehta", "ram kumar",
    # Other notable
    "whistler", "sargent", "sorolla", "joaquín sorolla",
    "ilya repin", "canaletto", "guardi",
}

# Normalized set for fast lookup (lowercase, stripped)
_MAJOR_ARTISTS_NORMALIZED = {name.lower().strip() for name in MAJOR_ARTISTS}


def is_major_artist(artist_name: str) -> bool:
    """
    Check if an artist name matches the curated list of major/well-known artists.

    Uses substring matching so "Claude Monet" matches "monet",
    "Vincent van Gogh" matches "van gogh", etc.
    """
    if not artist_name:
        return False
    artist_lower = artist_name.lower().strip()
    for known in _MAJOR_ARTISTS_NORMALIZED:
        if known in artist_lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Painting-only filter — skip drawings, prints, sketches, photographs
# ---------------------------------------------------------------------------
# Keywords in the "medium" or "classification" field that indicate
# the work is NOT a painting. If any of these appear, skip it.
_NON_PAINTING_KEYWORDS = {
    "drawing", "sketch", "graphite", "pencil", "charcoal", "chalk",
    "etching", "engraving", "lithograph", "woodcut", "print",
    "photograph", "photo", "gelatin", "albumen", "daguerreotype",
    "ink on paper", "pen and ink", "crayon", "pastel on paper",
    "screenprint", "woodblock", "aquatint", "mezzotint", "drypoint",
    "ceramic", "porcelain", "textile", "tapestry", "embroidery",
    "sculpture", "bronze", "marble", "terracotta", "ivory",
}

# Keywords that positively confirm it's a painting
_PAINTING_KEYWORDS = {
    "oil on canvas", "oil on panel", "oil on board", "oil on copper",
    "oil on wood", "tempera", "acrylic", "fresco", "gouache",
    "watercolor on", "watercolour on",
    "painting", "painted",
}


def is_painting(medium: str, classification: str = "") -> bool:
    """Return True if the artwork appears to be a painting (not a drawing, print, photo, etc.).

    If medium is empty/unknown, returns True (benefit of the doubt).
    """
    combined = f"{medium} {classification}".lower().strip()
    if not combined.strip():
        return True  # No info — let it through

    # Positive match: if it looks like a painting, accept immediately
    for kw in _PAINTING_KEYWORDS:
        if kw in combined:
            return True

    # Negative match: if it has non-painting keywords, reject
    for kw in _NON_PAINTING_KEYWORDS:
        if kw in combined:
            return False

    # No strong signal either way — let it through
    return True


# ---------------------------------------------------------------------------
# Met Museum API
# ---------------------------------------------------------------------------
MET_BASE = "https://collectionapi.metmuseum.org/public/collection/v1"
MET_SEARCH = f"{MET_BASE}/search"
MET_OBJECT = f"{MET_BASE}/objects"


def search_met(query: str, public_domain_only: bool = True, department_id: int = None) -> list[int]:
    """Search the Met and return a list of object IDs.

    department_id 11 = European Paintings, 21 = Modern Art — use these to
    avoid pulling back thousands of drawings, prints, and photographs.
    """
    params = {"q": query, "hasImages": True}
    if public_domain_only:
        params["isPublicDomain"] = True
    if department_id:
        params["departmentIds"] = department_id

    try:
        resp = requests.get(MET_SEARCH, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        object_ids = data.get("objectIDs") or []
        logger.info(f"Met search '{query}': {len(object_ids)} results")
        return object_ids
    except Exception as e:
        logger.error(f"Met search failed for '{query}': {e}")
        return []


def get_met_object(object_id: int) -> Optional[dict]:
    """Fetch a single Met object record with image URL and metadata."""
    try:
        resp = requests.get(f"{MET_OBJECT}/{object_id}", timeout=15)
        resp.raise_for_status()
        obj = resp.json()

        image_url = obj.get("primaryImage", "")
        if not image_url:
            return None

        return {
            "source": "met_museum",
            "id": str(object_id),
            "title": obj.get("title", "Untitled"),
            "artist": obj.get("artistDisplayName", "Unknown"),
            "date": obj.get("objectDate", ""),
            "medium": obj.get("medium", ""),
            "department": obj.get("department", ""),
            "image_url": image_url,
            "dimensions": obj.get("dimensions", ""),
            "culture": obj.get("culture", ""),
            "museum": "The Metropolitan Museum of Art",
        }
    except Exception as e:
        logger.error(f"Met object {object_id} fetch failed: {e}")
        return None


def fetch_random_met_artwork(
    queries: list[str], public_domain_only: bool = True, min_width: int = 1500,
    landscape_only: bool = True,
) -> Optional[dict]:
    """Pick a random query, search the Met, and return one random landscape artwork."""
    query = random.choice(queries)
    object_ids = search_met(query, public_domain_only)

    if not object_ids:
        return None

    # Try up to 10 random objects to find one with a good image
    random.shuffle(object_ids)
    for obj_id in object_ids[:10]:
        artwork = get_met_object(obj_id)
        if artwork and artwork["image_url"]:
            # Met doesn't give pixel dimensions in the API, so we can't filter here.
            # Filtering happens at download/processing time by checking actual image dims.
            return artwork
        time.sleep(0.1)  # Be respectful of rate limits

    return None


# ---------------------------------------------------------------------------
# Rijksmuseum API (new Search API — no API key needed)
# ---------------------------------------------------------------------------
RIJKS_SEARCH = "https://data.rijksmuseum.nl/search/collection"
RIJKS_DATA = "https://data.rijksmuseum.nl"


def search_rijksmuseum(
    query: str = "",
    types: list[str] = None,
) -> list[str]:
    """
    Search Rijksmuseum using the new public Search API (no key needed).

    Returns a list of object identifiers (URLs like https://id.rijksmuseum.nl/...).
    """
    params = {}
    if query:
        # The search API uses query params like type=, maker=, material=
        # For general text search, different params may apply
        params["maker"] = query
    if types:
        params["type"] = types[0]  # e.g., "painting"

    try:
        resp = requests.get(
            RIJKS_SEARCH,
            params=params,
            headers={"Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        # Response is JSON-LD with orderedItems containing identifier URLs
        items = data.get("orderedItems", [])
        # Items can be strings (URIs) or dicts with "id" field
        ids = []
        for item in items:
            if isinstance(item, str):
                ids.append(item)
            elif isinstance(item, dict):
                ids.append(item.get("id", ""))
        ids = [i for i in ids if i]
        logger.info(f"Rijksmuseum search '{query}': {len(ids)} results")
        return ids
    except Exception as e:
        logger.error(f"Rijksmuseum search failed for '{query}': {e}")
        return []


def resolve_rijks_object(object_uri: str) -> Optional[dict]:
    """
    Resolve a Rijksmuseum object identifier to get metadata and image URL.

    Takes an id like https://id.rijksmuseum.nl/200100988 and resolves it
    via the Linked Data resolver at data.rijksmuseum.nl.
    """
    try:
        # Convert id.rijksmuseum.nl URI to data.rijksmuseum.nl for resolution
        # e.g., https://id.rijksmuseum.nl/200100988 -> https://data.rijksmuseum.nl/200100988
        obj_id = object_uri.split("/")[-1]
        resolve_url = f"{RIJKS_DATA}/{obj_id}"

        resp = requests.get(
            resolve_url,
            headers={"Accept": "application/ld+json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        # Extract image URL from the Linked Art / Schema.org response
        # The structure varies; look for image references in common fields
        image_url = _extract_rijks_image_url(data, obj_id)
        if not image_url:
            return None

        # Extract metadata
        title = "Untitled"
        artist = "Unknown"

        # Try Linked Art fields
        if isinstance(data, dict):
            # Title: look in _label, label, or identified_by
            title = data.get("_label", data.get("label", "Untitled"))
            if isinstance(title, list):
                title = title[0] if title else "Untitled"

            # Artist: look in produced_by or created_by
            produced = data.get("produced_by", data.get("created_by", {}))
            if isinstance(produced, dict):
                carried_out = produced.get("carried_out_by", [])
                if isinstance(carried_out, list) and carried_out:
                    artist = carried_out[0].get("_label", "Unknown")
                elif isinstance(carried_out, dict):
                    artist = carried_out.get("_label", "Unknown")

        return {
            "source": "rijksmuseum",
            "id": obj_id,
            "title": str(title),
            "artist": str(artist),
            "date": "",
            "medium": "",
            "department": "",
            "image_url": image_url,
            "dimensions": "",
            "culture": "",
            "museum": "Rijksmuseum, Amsterdam",
        }
    except Exception as e:
        logger.error(f"Rijksmuseum resolve failed for {object_uri}: {e}")
        return None


def _extract_rijks_image_url(data: dict, obj_id: str) -> Optional[str]:
    """Extract the best image URL from a resolved Rijksmuseum object."""
    # Strategy 1: Look for representation or subject_of with IIIF
    for key in ("representation", "subject_of", "digitally_shown_by"):
        refs = data.get(key, [])
        if isinstance(refs, dict):
            refs = [refs]
        if isinstance(refs, list):
            for ref in refs:
                if isinstance(ref, dict):
                    ref_id = ref.get("id", "")
                    if "iiif" in ref_id or ref_id.endswith((".jpg", ".jpeg", ".png")):
                        return ref_id
                    # Check access_point for IIIF image service
                    access = ref.get("access_point", [])
                    if isinstance(access, dict):
                        access = [access]
                    if isinstance(access, list):
                        for ap in access:
                            if isinstance(ap, dict):
                                ap_id = ap.get("id", "")
                                if ap_id:
                                    return ap_id

    # Strategy 2: Construct a likely image URL from the object number
    # Rijksmuseum images are often at predictable IIIF endpoints
    # Try the common patterns
    for prefix in ("SK-", "RP-", "NG-", "BK-", "AK-"):
        if obj_id.startswith(prefix) or True:
            # Try the standard Rijksmuseum image URL pattern
            candidate = f"https://lh3.googleusercontent.com/proxy/{obj_id}"
            # This won't always work; the IIIF approach is more reliable

    # Strategy 3: Look for any URL-like string pointing to an image
    def find_image_urls(obj, depth=0):
        if depth > 5:
            return []
        urls = []
        if isinstance(obj, str):
            if any(ext in obj.lower() for ext in (".jpg", ".jpeg", ".png", "iiif")):
                urls.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                urls.extend(find_image_urls(v, depth + 1))
        elif isinstance(obj, list):
            for item in obj:
                urls.extend(find_image_urls(item, depth + 1))
        return urls

    image_urls = find_image_urls(data)
    if image_urls:
        # Prefer IIIF URLs, then any image URL
        iiif_urls = [u for u in image_urls if "iiif" in u.lower()]
        return iiif_urls[0] if iiif_urls else image_urls[0]

    return None


def fetch_random_rijks_artwork(
    queries: list[str],
    types: list[str] = None,
) -> Optional[dict]:
    """Pick a random query and return one random Rijksmuseum artwork."""
    query = random.choice(queries)
    object_ids = search_rijksmuseum(query, types)

    if not object_ids:
        return None

    # Try random objects to find one with a usable image
    random.shuffle(object_ids)
    for obj_uri in object_ids[:10]:
        artwork = resolve_rijks_object(obj_uri)
        if artwork and artwork["image_url"]:
            return artwork
        time.sleep(0.2)  # Be respectful

    return None


# ---------------------------------------------------------------------------
# Art Institute of Chicago API (no key needed)
# ---------------------------------------------------------------------------
AIC_SEARCH = "https://api.artic.edu/api/v1/artworks/search"
AIC_IIIF = "https://www.artic.edu/iiif/2"


def search_aic(
    query: str,
    limit: int = 100,
) -> list[dict]:
    """
    Search the Art Institute of Chicago for public domain paintings.
    Filters for artwork_type_title=Painting at the API level to avoid
    drawings, prints, and photographs in the results.
    Returns a list of artwork dicts with image_id for IIIF retrieval.
    """
    params = {
        "q": query,
        "limit": limit,
        "fields": "id,title,image_id,artist_title,date_display,is_public_domain,thumbnail,classification_title,medium_display,artwork_type_title",
        "query[term][is_public_domain]": "true",
        "query[term][artwork_type_title]": "Painting",
    }

    try:
        resp = requests.get(AIC_SEARCH, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        artworks = data.get("data", [])
        # Filter to only those with images
        artworks = [a for a in artworks if a.get("image_id")]
        logger.info(f"AIC search '{query}': {len(artworks)} results")
        return artworks
    except Exception as e:
        logger.error(f"AIC search failed for '{query}': {e}")
        return []


def fetch_random_aic_artwork(queries: list[str], landscape_only: bool = True) -> Optional[dict]:
    """Pick a random query and return one random landscape Art Institute of Chicago artwork."""
    query = random.choice(queries)
    results = search_aic(query)

    if not results:
        return None

    random.shuffle(results)
    for obj in results[:20]:
        image_id = obj.get("image_id")
        if not image_id:
            continue

        # Check aspect ratio via thumbnail dimensions (AIC provides these)
        thumb = obj.get("thumbnail", {}) or {}
        tw = thumb.get("width", 0)
        th = thumb.get("height", 0)
        if landscape_only and tw and th:
            if not is_landscape_enough(tw, th):
                logger.debug(f"AIC skip portrait/square: {obj.get('title', '')} ({tw}x{th})")
                continue

        # Build IIIF image URL — request max 3840px wide for 4K TV
        image_url = f"{AIC_IIIF}/{image_id}/full/3840,/0/default.jpg"

        return {
            "source": "aic",
            "id": str(obj.get("id", "")),
            "title": obj.get("title", "Untitled"),
            "artist": obj.get("artist_title", "Unknown"),
            "date": obj.get("date_display", ""),
            "medium": "",
            "department": "",
            "image_url": image_url,
            "dimensions": "",
            "culture": "",
            "museum": "Art Institute of Chicago",
        }

    return None


# ---------------------------------------------------------------------------
# Cleveland Museum of Art API (no key needed)
# ---------------------------------------------------------------------------
CMA_SEARCH = "https://openaccess-api.clevelandart.org/api/artworks/"


def search_cma(
    query: str,
    limit: int = 100,
    art_type: str = "Painting",
) -> list[dict]:
    """
    Search the Cleveland Museum of Art for artworks with images.
    Defaults to paintings only to avoid drawings, prints, and photographs.
    Returns artwork dicts with direct image URLs.
    """
    params = {
        "q": query,
        "has_image": 1,
        "limit": limit,
        "cc0": 1,  # Only CC0-licensed images
    }
    if art_type:
        params["type"] = art_type

    try:
        resp = requests.get(CMA_SEARCH, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        artworks = data.get("data", [])
        logger.info(f"CMA search '{query}': {len(artworks)} results")
        return artworks
    except Exception as e:
        logger.error(f"CMA search failed for '{query}': {e}")
        return []


def fetch_random_cma_artwork(queries: list[str], landscape_only: bool = True) -> Optional[dict]:
    """Pick a random query and return one random landscape Cleveland Museum artwork."""
    query = random.choice(queries)
    results = search_cma(query)

    if not results:
        return None

    random.shuffle(results)
    for obj in results[:20]:
        images = obj.get("images", {})
        if not images:
            continue

        # Prefer print resolution, fall back to web
        image_url = None
        img_w, img_h = 0, 0
        for key in ("print", "web", "full"):
            img_data = images.get(key, {})
            if img_data.get("url"):
                image_url = img_data["url"]
                img_w = img_data.get("width", 0)
                img_h = img_data.get("height", 0)
                break

        if not image_url:
            continue

        # Filter by aspect ratio if we have dimensions
        if landscape_only and img_w and img_h:
            if not is_landscape_enough(img_w, img_h):
                logger.debug(f"CMA skip portrait/square: {obj.get('title', '')} ({img_w}x{img_h})")
                continue

        # Extract creators
        creators = obj.get("creators", [])
        artist = creators[0].get("description", "Unknown") if creators else "Unknown"

        return {
            "source": "cma",
            "id": str(obj.get("id", "")),
            "title": obj.get("title", "Untitled"),
            "artist": artist,
            "date": obj.get("creation_date", ""),
            "medium": obj.get("technique", ""),
            "department": obj.get("department", ""),
            "image_url": image_url,
            "dimensions": "",
            "culture": obj.get("culture", [""])[0] if obj.get("culture") else "",
            "museum": "Cleveland Museum of Art",
        }

    return None


# ---------------------------------------------------------------------------
# Wikimedia Commons API (no key needed)
# ---------------------------------------------------------------------------
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Wikimedia requires a descriptive User-Agent or returns 403 Forbidden.
# See: https://meta.wikimedia.org/wiki/User-Agent_policy
_wiki_session = requests.Session()
_wiki_session.headers.update({
    "User-Agent": "FrameArtServer/1.0 (https://github.com/frame-art-server; open-source art display tool)",
})


def _get_category_files(category: str, limit: int = 50, max_depth: int = 2) -> list[str]:
    """Get file titles from a Wikimedia Commons category, recursing up to *max_depth* levels.

    Wikimedia organises large artist categories like "Paintings_by_Claude_Monet"
    into sub-sub-categories (by title -> individual paintings, by museum -> per-museum
    categories).  A single level of recursion isn't enough; we need at least 2.

    Museum-collection categories ("Landscape_paintings_in_the_Louvre") typically
    have century-based subcats with direct files, so depth=2 covers them too.

    Skips subcategories whose names contain 'stamps', 'details', 'framed',
    'unauthenticated', 'missing', 'ticket', or end in '.pdf'.
    """
    SKIP_KEYWORDS = {"stamps", "details", "framed", "unauthenticated", "missing", "ticket"}
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}

    def _should_skip(name: str) -> bool:
        low = name.lower()
        return any(kw in low for kw in SKIP_KEYWORDS)

    def _is_image_file(title: str) -> bool:
        """Return True if the file title looks like an image (not PDF, video, etc.)."""
        return any(title.lower().endswith(ext) for ext in IMAGE_EXTENSIONS)

    def _fetch_files(cat: str, n: int) -> list[str]:
        """Fetch up to *n* direct image file members from a category (skips PDFs, videos)."""
        params = {
            "action": "query", "format": "json",
            "list": "categorymembers",
            "cmtitle": f"Category:{cat}",
            "cmtype": "file", "cmlimit": n,
        }
        try:
            resp = _wiki_session.get(COMMONS_API, params=params, timeout=15)
            resp.raise_for_status()
            members = resp.json().get("query", {}).get("categorymembers", [])
            return [
                m["title"] for m in members
                if m.get("title", "").startswith("File:") and _is_image_file(m["title"])
            ]
        except Exception as e:
            logger.error(f"Wikimedia files fetch failed for '{cat}': {e}")
            return []

    def _fetch_subcats(cat: str) -> list[str]:
        """Fetch subcategory names for a category."""
        params = {
            "action": "query", "format": "json",
            "list": "categorymembers",
            "cmtitle": f"Category:{cat}",
            "cmtype": "subcat", "cmlimit": 50,
        }
        try:
            resp = _wiki_session.get(COMMONS_API, params=params, timeout=15)
            resp.raise_for_status()
            subcats = resp.json().get("query", {}).get("categorymembers", [])
            names = []
            for sc in subcats:
                name = sc.get("title", "").replace("Category:", "")
                if name and not _should_skip(name):
                    names.append(name)
            return names
        except Exception as e:
            logger.error(f"Wikimedia subcats fetch failed for '{cat}': {e}")
            return []

    def _walk(cat: str, depth: int, remaining: int) -> list[str]:
        """Recursively collect file titles up to *remaining* count."""
        if remaining <= 0 or depth < 0:
            return []

        # First, grab direct files
        files = _fetch_files(cat, remaining)

        # If we still need more and haven't hit max depth, recurse into subcategories
        if len(files) < remaining // 2 and depth > 0:
            subcats = _fetch_subcats(cat)
            per_subcat = max(3, (remaining - len(files)) // max(1, len(subcats)))
            for sc in subcats:
                new_files = _walk(sc, depth - 1, per_subcat)
                files.extend(new_files)
                time.sleep(0.15)
                if len(files) >= remaining:
                    break

        return files

    file_titles = _walk(category, max_depth, limit)

    # Deduplicate while preserving order
    return list(dict.fromkeys(file_titles))[:limit]


def search_wikimedia_commons(
    query: str,
    category: str = "",
    limit: int = 50,
) -> list[dict]:
    """
    Search Wikimedia Commons for public domain images.

    Can search by text query or by category (e.g., "Paintings_by_Claude_Monet").
    For categories, automatically recurses up to 2 levels into subcategories to
    find files (needed for deeply nested artist categories on Wikimedia).
    Returns a list of dicts with title, image URL, and metadata.
    """
    results = []

    if not category:
        # Use search API for text queries
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": 6,  # File namespace
            "gsrlimit": limit,
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata",
            "iiurlwidth": 3840,  # Request a 3840px-wide thumbnail
        }

    try:
        if category:
            # Get file titles from category (with subcategory recursion)
            file_titles = _get_category_files(category, limit)

            if not file_titles:
                logger.info(f"Wikimedia Commons category '{category}': 0 files found")
                return []

            logger.info(f"Wikimedia Commons category '{category}': {len(file_titles)} files found, fetching metadata...")

            # Get image info for those files (batch of up to 50)
            for i in range(0, len(file_titles), 50):
                batch = file_titles[i : i + 50]
                info_params = {
                    "action": "query",
                    "format": "json",
                    "titles": "|".join(batch),
                    "prop": "imageinfo",
                    "iiprop": "url|size|extmetadata",
                    "iiurlwidth": 3840,
                }
                resp2 = _wiki_session.get(COMMONS_API, params=info_params, timeout=15)
                resp2.raise_for_status()
                pages = resp2.json().get("query", {}).get("pages", {})
                for page in pages.values():
                    artwork = _parse_commons_page(page)
                    if artwork:
                        results.append(artwork)
                time.sleep(0.3)
        else:
            # Text search — imageinfo is already requested
            resp = _wiki_session.get(COMMONS_API, params=params, timeout=15)
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages", {})
            for page in pages.values():
                artwork = _parse_commons_page(page)
                if artwork:
                    results.append(artwork)

        logger.info(f"Wikimedia Commons '{query or category}': {len(results)} results")
        return results

    except Exception as e:
        logger.error(f"Wikimedia Commons search failed for '{query or category}': {e}")
        return []


def _parse_commons_page(page: dict) -> Optional[dict]:
    """Parse a Wikimedia Commons API page result into an artwork dict."""
    imageinfo = page.get("imageinfo", [{}])
    if not imageinfo:
        return None

    info = imageinfo[0]
    url = info.get("thumburl") or info.get("url", "")
    if not url:
        return None

    # Skip non-image files, tiny images, and non-landscape images
    width = info.get("width", 0)
    height = info.get("height", 0)
    if width < 800 or height < 600:
        return None
    if not is_landscape_enough(width, height):
        return None

    # Only accept common image formats
    lower_url = url.lower()
    if not any(lower_url.endswith(ext) for ext in (".jpg", ".jpeg", ".png")):
        # Try to get the original URL if thumb is in another format
        url = info.get("url", "")
        if not any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png")):
            return None

    # Extract metadata from extmetadata
    ext = info.get("extmetadata", {})
    title_raw = ext.get("ObjectName", {}).get("value", "")
    artist_raw = ext.get("Artist", {}).get("value", "")
    date_raw = ext.get("DateTimeOriginal", {}).get("value", "")
    description = ext.get("ImageDescription", {}).get("value", "")

    # Clean up HTML tags from artist field
    import re
    artist_clean = re.sub(r"<[^>]+>", "", artist_raw).strip() if artist_raw else ""

    # Use page title as fallback
    page_title = page.get("title", "").replace("File:", "").rsplit(".", 1)[0]
    page_title = page_title.replace("_", " ").strip()

    title = title_raw or page_title or "Untitled"
    artist = artist_clean or "Unknown"

    # Try to extract museum/institution from description or Credit
    credit = ext.get("Credit", {}).get("value", "")
    museum = ""
    if credit:
        credit_clean = re.sub(r"<[^>]+>", "", credit).strip()
        # Often the credit line contains the museum name
        if any(word in credit_clean.lower() for word in ["museum", "gallery", "collection", "institute"]):
            # Take just the institution name portion (usually first part before "-" or "–")
            museum = credit_clean.split(" - ")[0].split(" – ")[0].strip()
            if len(museum) > 80:
                museum = ""  # Too long, skip it

    return {
        "source": "wikimedia",
        "id": str(page.get("pageid", "")),
        "title": title,
        "artist": artist,
        "date": date_raw or "",
        "medium": "",
        "department": "",
        "image_url": url,
        "dimensions": "",
        "culture": "",
        "museum": museum or "Wikimedia Commons",
    }


def fetch_random_wikimedia_artwork(
    queries: list[str] = None,
    categories: list[str] = None,
) -> Optional[dict]:
    """Pick a random query/category and return one artwork from Wikimedia Commons."""
    results = []

    if categories:
        cat = random.choice(categories)
        results = search_wikimedia_commons("", category=cat)
    elif queries:
        query = random.choice(queries)
        results = search_wikimedia_commons(query)

    if not results:
        return None

    return random.choice(results)


# ---------------------------------------------------------------------------
# Local folder source
# ---------------------------------------------------------------------------
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}


def fetch_random_local_artwork(local_path: str) -> Optional[dict]:
    """Pick a random image from a local folder."""
    folder = Path(local_path)
    if not folder.is_dir():
        logger.warning(f"Local art folder not found: {local_path}")
        return None

    images = [
        f
        for f in folder.iterdir()
        if f.suffix.lower() in SUPPORTED_EXTENSIONS
    ]

    if not images:
        logger.warning(f"No images found in {local_path}")
        return None

    chosen = random.choice(images)
    return {
        "source": "local",
        "id": chosen.stem,
        "title": chosen.stem.replace("_", " ").replace("-", " ").title(),
        "artist": "",
        "date": "",
        "medium": "",
        "department": "",
        "image_url": str(chosen),  # Local path, not a URL
        "dimensions": "",
        "culture": "",
        "museum": "",
    }


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------
def download_image(url_or_path: str, cache_dir: str) -> Optional[str]:
    """Download an image to the cache directory. Returns local file path."""
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    # Local file — just return the path
    if os.path.isfile(url_or_path):
        return url_or_path

    # Generate a filename from the URL
    filename = url_or_path.split("/")[-1].split("?")[0]
    if not filename:
        filename = f"art_{hash(url_or_path) & 0xFFFFFFFF}.jpg"
    local_path = cache / filename

    if local_path.exists():
        logger.debug(f"Cache hit: {local_path}")
        return str(local_path)

    try:
        logger.info(f"Downloading: {url_or_path[:100]}...")
        resp = requests.get(url_or_path, timeout=60, stream=True)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info(f"Saved to: {local_path}")
        return str(local_path)
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return None
