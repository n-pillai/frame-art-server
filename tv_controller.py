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

    def __init__(self, ip: str, port: int = 8002, token_file: str = "tv_token.txt"):
        self.ip = ip
        self.port = port
        self.token_file = token_file
        self._tv = None
        self._art = None

    def _load_token(self) -> Optional[str]:
        """Load saved pairing token."""
        path = Path(self.token_file)
        if path.exists():
            token = path.read_text().strip()
            if token:
                logger.debug("Loaded saved TV token")
                return token
        return None

    def _save_token(self, token: str):
        """Save pairing token for future connections."""
        Path(self.token_file).write_text(token)
        logger.info("Saved TV pairing token")

    def connect(self) -> bool:
        """
        Connect to the Frame TV.

        On first connection, the TV will show a pairing prompt —
        you need to accept it with the TV remote. The token is then
        saved for future connections.
        """
        try:
            from samsungtvws import SamsungTVWS

            token = self._load_token()

            self._tv = SamsungTVWS(
                host=self.ip,
                port=self.port,
                token=token,
                name="FrameArtServer",
            )

            # Test the connection and get art API
            self._art = self._tv.art()

            # Save the token if we got a new one
            if self._tv.token and self._tv.token != token:
                self._save_token(self._tv.token)

            logger.info(f"Connected to Frame TV at {self.ip}:{self.port}")
            return True

        except ImportError:
            logger.error(
                "samsungtvws not installed. Run: "
                'pip install "samsungtvws[async,encrypted]"'
            )
            return False
        except Exception as e:
            logger.error(f"Failed to connect to TV at {self.ip}: {e}")
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
        """
        logger.info("Reconnecting to Frame TV...")
        self._tv = None
        self._art = None
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
