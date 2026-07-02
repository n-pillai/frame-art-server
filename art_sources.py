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
    # Use full/specific names for common surnames to avoid false matches
    # (e.g., "Constable" alone matches Lionel Constable, not just John)
    "j. m. w. turner", "j.m.w. turner", "joseph mallord william turner",
    "william turner",  # catches "Joseph Mallord William Turner"
    "john constable",  # not just "constable" — avoids Lionel Constable
    "delacroix", "géricault", "gericault",
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
    "ceramic", "porcelain", "tile", "tiles", "glazed",
    "textile", "tapestry", "embroidery",
    "sculpture", "bronze", "marble", "terracotta", "ivory",
    "stained glass", "glass", "enamel", "cloisonn",
    "furniture", "silver", "gold leaf on",
    "decorative art", "armor", "arms",
    "frieze", "fan mount", "architectural",
}

# Title keywords that suggest the work is a study or non-display piece
_NON_DISPLAY_TITLE_KEYWORDS = {
    "study for", "sketch for", "male nude", "female nude",
    "frieze fragment", "fan mount",
}

# Keywords that positively confirm it's a painting
_PAINTING_KEYWORDS = {
    "oil on canvas", "oil on panel", "oil on board", "oil on copper",
    "oil on wood", "tempera", "acrylic", "fresco", "gouache",
    "watercolor on", "watercolour on",
    "painting",
    # Note: "painted" deliberately excluded — "painted tile", "painted ceramic"
    # are not paintings. Only match specific painting media above.
}


def is_painting(medium: str, classification: str = "") -> bool:
    """Return True if the artwork appears to be a painting (not a drawing, print, photo, etc.).

    If medium is empty/unknown, returns True (benefit of the doubt).
    Checks negative keywords FIRST so "painted ceramic" is rejected
    before "painted" could match as a positive.
    """
    combined = f"{medium} {classification}".lower().strip()
    if not combined.strip():
        return True  # No info — let it through

    # Negative match FIRST: reject decorative arts, prints, etc.
    # This must come before positive matching so "painted tile" doesn't
    # slip through via the "painting" keyword.
    for kw in _NON_PAINTING_KEYWORDS:
        if kw in combined:
            return False

    # Positive match: if it looks like a painting, accept
    for kw in _PAINTING_KEYWORDS:
        if kw in combined:
            return True

    # No strong signal either way — let it through
    return True


def is_display_worthy(title: str) -> bool:
    """Return False if the title suggests it's a study, fragment, or non-display piece."""
    if not title:
        return True
    title_lower = title.lower()
    for kw in _NON_DISPLAY_TITLE_KEYWORDS:
        if kw in title_lower:
            return False
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


def get_met_object(object_id: int, max_retries: int = 3) -> Optional[dict]:
    """Fetch a single Met object record with image URL and metadata.

    Retries with exponential backoff on 403/429 (rate limiting).
    """
    for attempt in range(max_retries):
        try:
            resp = requests.get(f"{MET_OBJECT}/{object_id}", timeout=15)

            # Rate-limited — back off and retry
            if resp.status_code in (403, 429):
                wait = 2 ** attempt  # 1s, 2s, 4s
                logger.warning(f"Met rate-limited (HTTP {resp.status_code}) on object {object_id}, "
                               f"retrying in {wait}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue

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
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"Met object {object_id} fetch error: {e}, retrying in {wait}s")
                time.sleep(wait)
            else:
                logger.error(f"Met object {object_id} fetch failed after {max_retries} attempts: {e}")
                return None
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
    Search the Art Institute of Chicago for public domain artworks.
    Uses POST with Elasticsearch JSON body for reliable filtering.
    Painting filtering is done client-side via is_painting() in the gatherer.
    Returns a list of artwork dicts with image_id for IIIF retrieval.
    """
    # AIC's Elasticsearch API is more reliable with POST + JSON body
    # than with query-string term filters (which break at limit > ~30).
    payload = {
        "q": query,
        "limit": limit,
        "fields": [
            "id", "title", "image_id", "artist_title", "date_display",
            "is_public_domain", "thumbnail", "classification_title",
            "medium_display", "artwork_type_title",
        ],
        "query": {
            "bool": {
                "must": [
                    {"term": {"is_public_domain": True}},
                ],
            },
        },
    }

    try:
        resp = requests.post(AIC_SEARCH, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        artworks = data.get("data", [])
        # Filter to only those with images
        artworks = [a for a in artworks if a.get("image_id")]
        logger.info(f"AIC search '{query}': {len(artworks)} results")
        return artworks
    except Exception as e:
        # Fallback: simple GET without painting filter
        logger.warning(f"AIC POST search failed for '{query}': {e}, trying GET fallback...")
        try:
            params = {
                "q": query,
                "limit": limit,
                "fields": "id,title,image_id,artist_title,date_display,is_public_domain,thumbnail,classification_title,medium_display,artwork_type_title",
            }
            resp = requests.get(AIC_SEARCH, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            artworks = data.get("data", [])
            artworks = [a for a in artworks if a.get("image_id")]
            # Client-side public domain filter
            artworks = [a for a in artworks if a.get("is_public_domain")]
            logger.info(f"AIC search '{query}' (GET fallback): {len(artworks)} results")
            return artworks
        except Exception as e2:
            logger.error(f"AIC search failed for '{query}': {e2}")
            return []



# ---------------------------------------------------------------------------
# Cleveland Museum of Art API (no key needed)
# ---------------------------------------------------------------------------
CMA_SEARCH = "https://openaccess-api.clevelandart.org/api/artworks/"


def search_cma(
    query: str,
    limit: int = 100,
    art_type: str = "",
) -> list[dict]:
    """
    Search the Cleveland Museum of Art for artworks with images.
    Painting filtering is done client-side via is_painting() in the gatherer,
    because CMA's 'type' field is inconsistent and drops many valid paintings.
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


def _museum_from_category(category: str) -> str:
    """Infer the museum name from a Wikimedia Commons category string.

    e.g., "Landscape_paintings_in_the_Louvre"           -> "Musée du Louvre"
          "Paintings_in_the_National_Gallery,_London"    -> "National Gallery, London"
          "Paintings_in_the_Museo_del_Prado"             -> "Museo del Prado"
          "Paintings_by_Claude_Monet"                    -> ""  (artist, not museum)
    """
    # Category-to-museum lookup for the categories we actually use in config.
    # Much more reliable than guessing from the category name.
    _CAT_MAP = {
        "Landscape_paintings_in_the_Louvre":                "Musée du Louvre, Paris",
        "Paintings_in_the_Louvre":                          "Musée du Louvre, Paris",
        "Landscape_paintings_in_the_Musée_d'Orsay":        "Musée d'Orsay, Paris",
        "Paintings_in_the_Musée_d'Orsay":                  "Musée d'Orsay, Paris",
        "Paintings_in_the_National_Gallery,_London":        "National Gallery, London",
        "Landscape_paintings_in_the_National_Gallery_of_Art": "National Gallery of Art, Washington",
        "Paintings_in_the_National_Gallery_of_Art":         "National Gallery of Art, Washington",
        "Paintings_in_the_Museo_del_Prado":                "Museo del Prado, Madrid",
        "Paintings_in_the_Uffizi_Gallery":                 "Uffizi Gallery, Florence",
        "Paintings_in_the_Hermitage":                      "State Hermitage Museum, St. Petersburg",
        "Paintings_in_the_Musée_de_l'Orangerie":           "Musée de l'Orangerie, Paris",
        "Paintings_in_the_Alte_Pinakothek":                "Alte Pinakothek, Munich",
        "Paintings_in_the_Kunsthistorisches_Museum":       "Kunsthistorisches Museum, Vienna",
    }

    if category in _CAT_MAP:
        return _CAT_MAP[category]

    # For categories like "Paintings_in_the_XYZ", extract the institution
    cat_clean = category.replace("_", " ")
    for prefix in ("Landscape paintings in the ", "Paintings in the ",
                    "Collection of the ", "Works in the "):
        if cat_clean.startswith(prefix):
            return cat_clean[len(prefix):]

    # "Paintings_by_Claude_Monet" — artist category, no museum info
    return ""


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
                    # Inject source category so _parse_commons_page can
                    # infer the museum from it (e.g., "Paintings_in_the_Louvre")
                    page["_source_category"] = category
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

    # Strip ALL HTML tags and Wikidata markup from every metadata field.
    # Wikimedia extmetadata often contains <div>, <span>, <a>, <img> tags,
    # hidden Wikidata QS: entries, and other markup that must not appear
    # in the on-screen label.
    import re

    def _strip_html(raw: str) -> str:
        """Remove HTML tags, collapse whitespace, strip hidden QS/Wikidata spans."""
        if not raw:
            return ""
        # Remove entire <div style="display: none;">...</div> blocks (Wikidata junk)
        clean = re.sub(r'<div[^>]*style="display:\s*none[^"]*"[^>]*>.*?</div>', "", raw, flags=re.DOTALL | re.IGNORECASE)
        # Remove all remaining HTML tags
        clean = re.sub(r"<[^>]+>", "", clean)
        # Remove Wikidata QS: prefixes that sometimes survive
        clean = re.sub(r"\bQS:\S+", "", clean)
        # Collapse whitespace
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean

    artist_clean = _strip_html(artist_raw)
    title_clean = _strip_html(title_raw)
    date_clean = _strip_html(date_raw)

    # Use page title as fallback
    page_title = page.get("title", "").replace("File:", "").rsplit(".", 1)[0]
    page_title = page_title.replace("_", " ").strip()

    title = title_clean or page_title or "Untitled"
    artist = artist_clean or "Unknown"

    # Try to extract the real museum/institution from multiple fields.
    # Wikimedia Commons pages can encode the source institution in:
    #   1. Credit field (e.g., "Rijksmuseum, Amsterdam")
    #   2. Categories (e.g., "Paintings_in_the_Louvre")
    #   3. Description (e.g., "This painting is in the National Gallery")
    museum = ""
    _MUSEUM_KEYWORDS = ("museum", "gallery", "collection", "institute",
                        "pinakothek", "uffizi", "prado", "hermitage",
                        "louvre", "orsay", "orangerie", "rijksmuseum",
                        "national gallery", "tate", "kunsthistorisches")

    # 1. Try Credit field first (most reliable)
    credit = ext.get("Credit", {}).get("value", "")
    if credit:
        credit_clean = _strip_html(credit)
        if any(kw in credit_clean.lower() for kw in _MUSEUM_KEYWORDS):
            museum = credit_clean.split(" - ")[0].split(" – ")[0].strip()
            if len(museum) > 80:
                museum = ""

    # 2. Try to infer from the category the item was found via.
    #    The page's categories are embedded by the caller using the
    #    special _source_category key if present.
    if not museum:
        src_cat = page.get("_source_category", "")
        if src_cat:
            museum = _museum_from_category(src_cat)

    # 3. Try the description as last resort
    if not museum and description:
        desc_clean = _strip_html(description)
        for kw in _MUSEUM_KEYWORDS:
            idx = desc_clean.lower().find(kw)
            if idx != -1:
                # Extract the sentence fragment containing the keyword
                # Look for the museum name: scan backwards to sentence start
                start = max(0, desc_clean.rfind(".", 0, idx) + 1)
                end = desc_clean.find(".", idx)
                if end == -1:
                    end = min(len(desc_clean), idx + 80)
                fragment = desc_clean[start:end].strip()
                if 5 < len(fragment) < 80:
                    museum = fragment
                break

    return {
        "source": "wikimedia",
        "id": str(page.get("pageid", "")),
        "title": title,
        "artist": artist,
        "date": date_clean or "",
        "medium": "",
        "department": "",
        "image_url": url,
        "dimensions": "",
        "culture": "",
        "museum": museum,  # empty string if unknown — sanitize_label handles it
    }


# ---------------------------------------------------------------------------
# Local folder source
# ---------------------------------------------------------------------------
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}


def gather_local_artworks(local_path: str) -> list[dict]:
    """Gather every supported image in a local folder as artwork dicts."""
    folder = Path(local_path)
    if not folder.is_dir():
        logger.warning(f"Local art folder not found: {local_path}")
        return []

    artworks = []
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        artworks.append({
            "source": "local",
            "id": f.stem,
            "title": f.stem.replace("_", " ").replace("-", " ").title(),
            "artist": "",
            "date": "",
            "medium": "",
            "department": "",
            "image_url": str(f),  # Local path, not a URL
            "dimensions": "",
            "culture": "",
            "museum": "",
        })

    if not artworks:
        logger.warning(f"No images found in {local_path}")
    else:
        logger.info(f"Local folder '{local_path}': {len(artworks)} images gathered")
    return artworks


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
        # Use the Wikimedia session (with proper User-Agent) for Wikimedia URLs,
        # otherwise Wikimedia returns 403 per their UA policy.
        if "wikimedia.org" in url_or_path or "wikipedia.org" in url_or_path:
            session = _wiki_session
        else:
            session = requests
        resp = session.get(url_or_path, timeout=60, stream=True)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info(f"Saved to: {local_path}")
        return str(local_path)
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return None