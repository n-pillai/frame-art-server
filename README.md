# Frame Art Server

Gallery-quality art for your Samsung Frame TV — powered by museum APIs, no subscription needed.

Pulls public domain masterpieces from five museum sources (including a Wikimedia Commons gateway to the Louvre, Orsay, Prado, and more), filters for landscape-only works by major artists, processes them to 4K with metadata labels, and outputs them ready for your Frame TV via USB.

---

## How It Works

Run a script on your laptop, copy the output to a USB drive, plug it into the TV. No server, no Pi, no subscription, no ongoing maintenance.

```
Museum APIs  ──►  Filter  ──►  Download  ──►  Process  ──►  USB / TV
(5 sources)      (landscape,   (hi-res       (4K crop,     (plug in
                  major         originals)     sharpen,      and go)
                  artists)                     label)
```

### Art Sources

The system pulls from five free, no-API-key-needed sources:

- **Metropolitan Museum of Art** — 490,000+ public domain works (CC0)
- **Rijksmuseum** — 800,000+ works including Vermeer, Rembrandt, Ruisdael
- **Art Institute of Chicago** — major Impressionist collection (IIIF image service)
- **Cleveland Museum of Art** — strong landscape and European painting collection
- **Wikimedia Commons** — gateway to museums without their own APIs:
  - Musée du Louvre
  - Musée d'Orsay
  - National Gallery, London
  - National Gallery of Art (Washington, D.C.)
  - Museo del Prado
  - Uffizi Gallery
  - Hermitage Museum
  - Musée de l'Orangerie
  - Alte Pinakothek
  - Kunsthistorisches Museum

Wikimedia categories are walked recursively (up to 2 levels deep) to find actual image files within deeply nested artist and museum collection categories.

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
python batch_build.py --count 300 --output /Volumes/USB_DRIVE/frame_art
```

The script searches all configured sources, downloads high-res public domain originals, filters for landscape orientation and major artists, processes each to 3840x2160 with a metadata label, and saves to `./frame_tv_art/` (or your chosen directory).

### 3. Load onto the Frame TV

**USB method:**
1. Copy the `frame_tv_art` folder to a USB drive
2. Plug USB into the One Connect Box
3. On the TV: Menu > Art Mode > My Photos > import from USB
4. Set to shuffle slideshow

**SmartThings app method:**
1. Open SmartThings > select your Frame TV > Art Mode > Add Your Photos
2. Select images from your phone/tablet

### 4. Refresh whenever you want

Run the script again for a fresh batch. Set `major_artists_only: false` in config.yaml for a broader, more eclectic mix beyond the "greatest hits."

---

## Filtering

### Landscape Only

Only images with an aspect ratio of 1.3 or wider (roughly 4:3 and up) are accepted. Portraits, squares, and near-square images are automatically skipped at every stage of the pipeline — during API queries, after download, and again during processing. This means every image fills the 16:9 frame naturally with only a gentle center crop, no awkward stretching or heavy cropping.

### Major Artists Only

When `major_artists_only: true` (the default), only works by ~90 well-known artists are accepted. The list spans Impressionism (Monet, Renoir, Cézanne), Dutch Masters (Rembrandt, Vermeer, Ruisdael), Renaissance (Leonardo, Raphael, Titian), Romanticism (Turner, Constable, Delacroix), Hudson River School (Church, Cole, Bierstadt), Japanese art (Hokusai, Hiroshige), Modern (Klimt, Kandinsky, Matisse, Hopper), Indian art (Raja Ravi Varma, Amrita Sher-Gil), and others.

Set to `false` for a wider net that includes lesser-known artists.

---

## Image Processing

Every image goes through a gallery-quality pipeline:

1. **Landscape check** — rejects anything with aspect ratio below 1.3
2. **Size validation** — rejects images below 1500x1000px
3. **Center crop to 16:9** — gentle crop using the center of the composition
4. **4K resize** — Lanczos resampling to 3840x2160
5. **Sharpening** — subtle unsharp mask tuned for TV viewing distance
6. **Metadata label** — title, artist, and museum name overlaid in the top-left corner with a dark semi-transparent scrim (35% opacity, rounded rectangle with feathered edges). Font sizes are calibrated for readability on a 65" TV at normal viewing distance.

---

## Configuration

Edit `config.yaml` to customize:

**Art sources** — what gets pulled and from where:
```yaml
art_sources:
  major_artists_only: true    # "greatest hits" mode

  met_museum:
    queries:
      - "Claude Monet"
      - "Vincent van Gogh"
      - "Winslow Homer"

  wikimedia_commons:
    categories:
      - "Landscape_paintings_in_the_Louvre"
      - "Paintings_by_Claude_Monet"
      - "Paintings_in_the_Hermitage"
    queries:
      - "Turner landscape painting"
      - "Sorolla beach painting"
```

**Display settings:**
```yaml
display:
  resolution: [3840, 2160]    # 4K (use [1920, 1080] for 32" Frame)
  aspect_mode: "crop"         # center-crop to fill (landscape images only)
```

**Metadata overlay:**
```yaml
overlay:
  enabled: true
  position: "top_left"        # label with dark scrim
  opacity: 0.90
```

---

## Advanced: Raspberry Pi Daemon

For fully automated rotation (new art pushed to the TV on a schedule):

```bash
sudo bash install.sh
sudo nano /opt/frame-art-server/config.yaml   # set TV IP
sudo venv/bin/python frame_art_server.py --once  # pair with TV
sudo systemctl start frame-art                   # run as daemon
```

The Pi mode supports time-of-day scheduling with different art moods (bright mornings, warm evenings, calm nights) — see `schedule.time_slots` in config.yaml.

---

## Troubleshooting

**Images look wrong on the TV** — Check `display.resolution` in config.yaml. Use `[3840, 2160]` for all Frame TVs except the 32" model (which needs `[1920, 1080]`).

**"Skipping non-landscape image" warnings** — Working as intended. The pipeline rejects portraits and square images so only properly filling landscapes make it to the TV.

**"Skipping minor artist" log messages** — Set `major_artists_only: false` in config.yaml if you want a broader mix.

**Very few Wikimedia results** — Some categories are deeply nested. The code recurses 2 levels into subcategories, but some collections may need specific category names. Check the Wikimedia Commons category browser to find the right name.

---

## Project Structure

```
frame-art-server/
├── batch_build.py         # ← START HERE — batch download + process
├── frame_art_server.py    # Advanced: Pi daemon with live rotation
├── art_sources.py         # Museum API integrations (Met, Rijks, AIC, CMA, Wikimedia)
├── image_processor.py     # 4K processing, crop, sharpen, metadata overlay
├── tv_controller.py       # Samsung Frame TV WebSocket interface
├── scheduler.py           # Time-based rotation logic (Pi mode)
├── config.yaml            # Your configuration
├── requirements.txt       # Python dependencies
└── install.sh             # Pi installer
```

---

## License

The art itself is public domain (CC0 or equivalent). This tool is open source.
