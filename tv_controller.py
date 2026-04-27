"""
tv_controller.py — Interface with Samsung Frame TV via samsungtvws.

Handles:
  - Connecting to the TV (WebSocket)
  - First-time pairing / token management
  - Uploading art to the TV's internal storage
  - Setting the currently displayed artwork
  - Controlling art mode and slideshow
  - Applying matte styles via the TV API
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("frame_art.tv")


class FrameTVController:
    """Control a Samsung Frame TV's art mode via local WebSocket API."""

    # Backoff schedule (seconds) when the TV is unreachable. Capped at the
    # last entry so we keep retrying every 10 minutes indefinitely.
    _RETRY_BACKOFF_SECONDS = (30, 60, 120, 240, 600)

    def __init__(self, ip: str, port: int = 8002, token_file: str = "tv_token.txt"):
        self.ip = ip
        self.port = port
        # Resolve to absolute so daemons launched from a different cwd still
        # find (and update) the same token file.
        self.token_file = str(Path(token_file).resolve())
        self._tv = None
        self._art = None
        self._connected = False
        self._consecutive_failures = 0
        self._next_retry_at = 0.0

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        """
        Connect to the Frame TV and verify Art Mode is responsive.

        On first connection, the TV will show a pairing prompt —
        accept it with the TV remote. The library saves the token to
        ``token_file`` for future connections.

        Returns True only if both the WebSocket connects *and* Art Mode
        responds. A TV that is fully powered off may accept the WebSocket
        but fail the Art Mode check; treat that as not connected.
        """
        try:
            from samsungtvws import SamsungTVWS

            self._tv = SamsungTVWS(
                host=self.ip,
                port=self.port,
                token_file=self.token_file,
                name="FrameArtServer",
            )
            self._art = self._tv.art()

            if not self._art.supported():
                logger.warning(
                    "Connected to TV but Art Mode is not responding "
                    "(TV may be powered off or not a Frame TV)."
                )
                self._connected = False
                return False

            logger.info(f"Connected to Frame TV at {self.ip}:{self.port}")
            self._connected = True
            return True

        except ImportError:
            logger.error(
                "samsungtvws not installed. Run: "
                'pip install "samsungtvws[async,encrypted]"'
            )
            self._connected = False
            return False
        except Exception as e:
            logger.warning(f"Failed to connect to TV at {self.ip}: {e}")
            self._connected = False
            return False

    def ensure_connected(self) -> bool:
        """
        Try to (re)establish a working TV connection if currently offline.

        Uses exponential backoff (30s, 1m, 2m, 4m, 10m, 10m, ...) so a TV
        that's been off for hours doesn't generate an attempt every minute.
        Returns True if connected (or was already connected).
        """
        if self._connected:
            return True

        now = time.monotonic()
        if now < self._next_retry_at:
            return False

        if self.connect():
            self._consecutive_failures = 0
            self._next_retry_at = 0.0
            return True

        self._consecutive_failures += 1
        idx = min(self._consecutive_failures - 1, len(self._RETRY_BACKOFF_SECONDS) - 1)
        delay = self._RETRY_BACKOFF_SECONDS[idx]
        self._next_retry_at = now + delay
        logger.info(
            f"TV offline (attempt {self._consecutive_failures}). "
            f"Next reconnect in {delay}s."
        )
        return False

    def is_art_mode_supported(self) -> bool:
        """Check if this TV supports Art Mode (i.e., it's a Frame TV)."""
        try:
            return self._art.supported()
        except Exception as e:
            logger.error(f"Art mode support check failed: {e}")
            return False

    def get_art_mode_status(self) -> Optional[str]:
        """Get current art mode status."""
        try:
            return self._art.get_artmode()
        except Exception as e:
            logger.error(f"Failed to get art mode status: {e}")
            return None

    def set_art_mode(self, on: bool = True):
        """Turn art mode on or off."""
        try:
            self._art.set_artmode(on)
            logger.info(f"Art mode {'enabled' if on else 'disabled'}")
        except Exception as e:
            logger.error(f"Failed to set art mode: {e}")

    def list_uploaded_art(self) -> list:
        """List all artwork currently stored on the TV."""
        try:
            art_list = self._art.available()
            if isinstance(art_list, str):
                art_list = json.loads(art_list)
            logger.info(f"Found {len(art_list)} artworks on TV")
            return art_list
        except Exception as e:
            logger.error(f"Failed to list art: {e}")
            return []

    def reconnect(self) -> bool:
        """
        Drop the current connection and reconnect to the TV.

        Called automatically on broken pipe or stale connection errors.
        Bypasses the ensure_connected backoff window — this is an explicit
        recovery attempt triggered by a known-broken socket.
        """
        logger.info("Reconnecting to Frame TV...")
        self._tv = None
        self._art = None
        self._connected = False
        self._next_retry_at = 0.0
        return self.connect()

    def upload_image(
        self,
        image_path: str,
        matte_type: str = "none",
        _retry: bool = True,
    ) -> Optional[str]:
        """
        Upload an image to the Frame TV.

        Args:
            image_path: Path to the processed image file (JPG/PNG)
            matte_type: TV matte style to apply
                Options: none, modernthin, modern, modernwide, flexible,
                         shadowbox, panoramic, triptych, mix, squares

        Returns:
            The content_id of the uploaded image, or None on failure.
        """
        path = Path(image_path)
        if not path.exists():
            logger.error(f"Image not found: {image_path}")
            return None

        try:
            file_type = "JPEG" if path.suffix.lower() in (".jpg", ".jpeg") else "PNG"

            with open(image_path, "rb") as f:
                image_data = f.read()

            content_id = self._art.upload(
                image_data,
                file_type=file_type,
                matte=matte_type,
            )

            logger.info(
                f"Uploaded {path.name} → content_id: {content_id} "
                f"(matte: {matte_type})"
            )
            return content_id

        except BrokenPipeError as e:
            logger.warning(f"Broken pipe during upload ({e}). Reconnecting and retrying...")
            if _retry and self.reconnect():
                time.sleep(2)
                return self.upload_image(image_path, matte_type=matte_type, _retry=False)
            logger.error(f"Upload failed after reconnect attempt: {image_path}")
            return None

        except Exception as e:
            err_str = str(e).lower()
            if _retry and any(kw in err_str for kw in ("broken pipe", "connection reset", "connection refused", "timed out", "websocket")):
                logger.warning(f"Connection error during upload ({e}). Reconnecting and retrying...")
                if self.reconnect():
                    time.sleep(2)
                    return self.upload_image(image_path, matte_type=matte_type, _retry=False)
            logger.error(f"Upload failed for {image_path}: {e}")
            return None

    def set_active_art(self, content_id: str) -> bool:
        """Set a specific uploaded artwork as the currently displayed one."""
        try:
            self._art.select_image(content_id)
            logger.info(f"Set active art: {content_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to set active art {content_id}: {e}")
            return False

    def delete_art(self, content_id: str) -> bool:
        """Delete an uploaded artwork from the TV."""
        try:
            self._art.delete(content_id)
            logger.info(f"Deleted art: {content_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete art {content_id}: {e}")
            return False

    def set_slideshow(
        self,
        content_ids: list[str] = None,
        shuffle: bool = True,
        interval_minutes: int = 60,
    ):
        """
        Configure the TV's built-in slideshow/rotation.

        Args:
            content_ids: List of content IDs to include (None = all uploaded)
            shuffle: Randomize order
            interval_minutes: Time between image changes
        """
        try:
            # Convert minutes to the format the API expects
            # The API uses seconds or a specific format depending on firmware
            self._art.set_slideshow_status(
                {
                    "type": "shuffleSlideshow" if shuffle else "slideshow",
                    "value": interval_minutes * 60,
                }
            )
            logger.info(
                f"Slideshow configured: shuffle={shuffle}, "
                f"interval={interval_minutes}min"
            )
        except Exception as e:
            logger.error(f"Failed to configure slideshow: {e}")

    def cleanup_old_art(self, keep_ids: set[str] = None, max_on_tv: int = 50):
        """
        Remove old uploaded art to free space on the TV.

        Keeps art in keep_ids plus any Samsung-provided art.
        """
        try:
            all_art = self.list_uploaded_art()
            uploaded = [
                a for a in all_art
                if a.get("category_id") == "MY-C0004"  # User-uploaded category
            ]

            if len(uploaded) <= max_on_tv:
                return

            # Sort by upload date if available, remove oldest
            to_remove = []
            for art in uploaded:
                cid = art.get("content_id", "")
                if keep_ids and cid in keep_ids:
                    continue
                to_remove.append(cid)

            # Keep removing until we're under the limit
            removed = 0
            for cid in to_remove[: len(uploaded) - max_on_tv]:
                if self.delete_art(cid):
                    removed += 1

            logger.info(f"Cleaned up {removed} old artworks from TV")

        except Exception as e:
            logger.error(f"Cleanup failed: {e}")
