# Frame Art Server

Gallery-quality art for your Samsung Frame TV — powered by museum APIs, no subscription needed.

Pulls public domain masterpieces from four museum APIs plus Wikimedia Commons (a gateway to the Louvre, Orsay, Prado, and dozens more), filters for landscape works by major artists, processes them to 4K with metadata labels, and outputs them ready for your Frame TV via USB.

A free, open-source alternative to Samsung's $5.99/month Art Store subscription.

---

## How It Works

```
Museum APIs  -->  Filter  -->  Download  -->  Process  -->  USB --> TV
(4 APIs +        (landscape,   (hi-res       (4K crop,     (plug in
 Wikimedia)       major         originals)    sharpen,       and go)
                  artists)                    sRGB, label)
```

### Art Sources

The system pulls from free, no-API-key-needed sources:

- **Metropolitan Museum of Art** — 490,000+ public domain works (CC0)
- **Art Institute of Chicago** — major Impressionist collection (IIIF image service)
- **Cleveland Museum of Art** — strong landscape and European painting collection
- **Wikimedia Commons** — gateway to museums without their own APIs:
  - Musee du Louvre, Musee d'Orsay, Musee de l'Orangerie
  - National Gallery, London
  - National Gallery of Art, Washington D.C.
  - Museo del Prado, Uffizi Gallery
  - Hermitage Museum
  - Alte Pinakothek, Kunsthistorisches Museum
  - Plus artist-specific categories (Monet, Turner, Constable, Aivazovsky, etc.)

---

## Quick Start

### 1. Install dependencies

```bash
pip install Pillow requests pyyaml
```

### 2. Build your art collection

```bash
# Pull ~200 gallery-ready images
python batch_build.py --count 200

# Or save directly to a USB drive
python batch_build.py --count 300 --output E:\frame_art
```

The script searches all configured sources, downloads high-res public domain originals, filters for landscape orientation and major artists, converts colors to sRGB, processes each to 3840x2160 with a metadata label, and saves to `./frame_tv_art/`.

### 3. Load onto the Frame TV

1. Copy the `frame_tv_art` folder to a USB drive (FAT32 or exFAT)
2. Plug USB into the One Connect Box
3. On the TV: Menu > Art Mode > My Photos > import from USB
4. Set each image's mat to "No Mat" (unfortunately Samsung has no global setting for this)
5. Set to shuffle slideshow

### 4. Refresh whenever you want

Run the script again for a fresh batch. Set `major_artists_only: false` in config.yaml for a broader, more eclectic mix beyond the "greatest hits."

---

## Filtering

### Landscape Only

Only images with an aspect ratio of 1.3 or wider (roughly 4:3 and up) are accepted. Portraits, squares, and near-square images are automatically skipped. Every image fills the 16:9 frame naturally with only a gentle center crop.

### Major Artists Only

When `major_artists_only: true` (the default), only works by ~90 well-known artists are accepted. The list spans Impressionism, Dutch Masters, Renaissance, Romanticism, Hudson River School, Japanese art, Modern, Indian art, and others.

### Painting Filter

Non-paintings (drawings, prints, photographs, ceramics, sculptures, textiles, etc.) are filtered out using medium and classification metadata. Studies, fragments, and fan mounts are also excluded by title.

### Per-Artist Cap

No single artist can have more than `max_per_artist` works (default: 4) in a batch, preventing any one artist from dominating the gallery. Featured artists have their own caps.

### Featured Artists

Specify artists who should always have a minimum number of works in every batch:

```yaml
featured_artists:
  - name: "Raja Ravi Varma"
    min_count: 3
  - name: "Amrita Sher-Gil"
    min_count: 2
```

---

## Image Processing

Every image goes through a gallery-quality pipeline:

1. **Color profile conversion** — ICC profiles (Adobe RGB, ProPhoto, etc.) are converted to sRGB so colors display correctly on the TV
2. **Landscape check** — rejects anything with aspect ratio below 1.3
3. **Size validation** — rejects images below 1500x1000px
4. **Center crop to 16:9** — gentle crop using the center of the composition
5. **4K resize** — Lanczos resampling to exactly 3840x2160
6. **Sharpening** — subtle unsharp mask tuned for TV viewing distance
7. **Warmth adjustment** — slight warm shift to match the Frame TV's display characteristics
8. **Metadata label** — title, artist, date, and museum overlaid with a dark semi-transparent scrim. Font sizes calibrated for a 65" TV at normal viewing distance
9. **Metadata stripping** — all EXIF, ICC profiles, and orientation flags are removed from the output file

### Label Sanitization

Metadata from museum APIs is cleaned before display: HTML tags stripped, non-English titles translated or removed, URLs and Wikidata markup filtered, artist bios truncated, "Unknown date" suppressed, and museum names validated (no "Wikimedia Commons" or "Google Cultural Institute").

---

## Configuration

Edit `config.yaml` to customize:

**Art sources** — what gets pulled and from where:
```yaml
art_sources:
  major_artists_only: true    # "greatest hits" mode
  max_per_artist: 4           # variety cap

  met_museum:
    queries:
      - "Claude Monet"
      - "landscape painting"

  wikimedia_commons:
    categories:
      - "Landscape_paintings_in_the_Louvre"
      - "Paintings_by_Claude_Monet"
    queries:
      - "Turner landscape painting"
```

**Display settings:**
```yaml
display:
  resolution: [3840, 2160]    # 4K (use [1920, 1080] for 32" Frame)
  aspect_mode: "crop"         # center-crop to fill
```

**Metadata overlay:**
```yaml
overlay:
  enabled: true
  position: "top_left"
  opacity: 0.90
```

---

## Project Structure

```
frame-art-server/
  batch_build.py       # Main script -- run this to build a batch
  art_sources.py       # Museum API integrations (Met, AIC, CMA, Wikimedia)
  image_processor.py   # 4K processing, crop, sharpen, sRGB, metadata overlay
  config.yaml          # Configuration (queries, sources, display settings)
  requirements.txt     # Python dependencies
```

---

## Known Limitations

**Samsung Frame TV mat/border:** The TV defaults to showing a mat border on every image and there is no global "no mat" setting. You need to set each image to "No Mat" individually through the TV's menu. Samsung has acknowledged this as a limitation but hasn't addressed it in firmware updates.

**Wikimedia museum attribution:** When art comes through Wikimedia Commons, the museum name is inferred from the category or credit metadata. Some images may show no museum if the source metadata is incomplete.

---

## License

The art itself is public domain (CC0 or equivalent). This tool is open source.
