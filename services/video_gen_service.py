"""services/video_gen_service.py — Scene-level TTS, image gen, and video assembly.

Used by the admin video-storyboard editing page for per-scene previews.
"""
import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional
from urllib.parse import urljoin

import requests

from core.config import settings

logger = logging.getLogger(__name__)


# ── BRONCI TTS ─────────────────────────────────────────────────────────────────

class BronciTTSClient:
    """Client for the BRONCI TTS API (v2.4)."""

    def __init__(self, username: str, password: str, base_url: str):
        self.base_url = base_url
        self._token: Optional[str] = None
        self._expiration: int = 0
        self._username = username
        self._password = password

    def _ensure_token(self):
        if self._token and __import__("time").time() < self._expiration:
            return
        url = urljoin(self.base_url, "/api/v1/tts/login")
        resp = requests.post(url, json={
            "username": self._username,
            "password": self._password,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 200:
            raise ValueError(f"BRONCI login failed: {data.get('error', 'unknown')}")
        self._token = data["token"]
        self._expiration = __import__("time").time() + data.get("expiration", 3600)

    def synthesize(self, text: str, voice_id: str = "cmn-TW-vs2-F04",
                   speaking_rate: float = 1.0) -> bytes:
        self._ensure_token()
        url = urljoin(self.base_url, "/api/v1/tts/synthesize")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        payload = {
            "input": {"text": text, "textType": "common"},
            "voice": {"model": "broncitts", "languageCode": "cmn-TW", "name": voice_id},
            "audioConfig": {"speakingRate": speaking_rate, "sampleRate": 16000},
            "outputConfig": {"streamMode": 0},
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        return resp.content


def synthesize_speech(text: str, voice_id: str = "cmn-TW-vs2-F04",
                      speaking_rate: float = 1.0) -> bytes:
    """Synthesize text to WAV bytes via BRONCI TTS."""
    client = BronciTTSClient(
        settings.bronci_username,
        settings.bronci_password,
        settings.bronci_base_url,
    )
    return client.synthesize(text, voice_id=voice_id, speaking_rate=speaking_rate)


# ── Hugging Face Image Generation ──────────────────────────────────────────────

def generate_image(prompt: str) -> bytes:
    """Generate image via Hugging Face Inference API. Returns JPEG bytes."""
    url = f"https://router.huggingface.co/hf-inference/models/{settings.hf_image_model}"
    headers = {"Authorization": f"Bearer {settings.hf_api_token}"}
    payload = {
        "inputs": prompt,
        "parameters": {
            "negative_prompt": "blurry, low quality, distorted, text, watermark, signature",
            "width": 1024,
            "height": 576,
            "num_inference_steps": 4,
        },
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    if "image" not in content_type:
        raise ValueError(f"Unexpected response type: {content_type}")
    return resp.content


# ── Scene Video Assembly ───────────────────────────────────────────────────────

def assemble_scene_video(audio_bytes: bytes, image_bytes: bytes) -> Optional[bytes]:
    """Use FFmpeg to combine image + audio into an MP4 clip.

    Returns MP4 bytes, or None if FFmpeg is not available.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("FFmpeg not available for scene video assembly")
        return None

    tmpdir = tempfile.mkdtemp(prefix="scene_vid_")
    try:
        img_path = os.path.join(tmpdir, "input.jpg")
        wav_path = os.path.join(tmpdir, "input.wav")
        out_path = os.path.join(tmpdir, "output.mp4")

        with open(img_path, "wb") as f:
            f.write(image_bytes)
        with open(wav_path, "wb") as f:
            f.write(audio_bytes)

        result = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
             "-loop", "1", "-i", img_path, "-i", wav_path,
             "-c:v", "libx264", "-tune", "stillimage",
             "-b:v", "2M",
             "-c:a", "aac", "-b:a", "192k",
             "-pix_fmt", "yuv420p", "-shortest",
             "-movflags", "+faststart",
             out_path],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            logger.error(f"FFmpeg scene assembly failed: {result.stderr}")
            return None

        with open(out_path, "rb") as f:
            return f.read()
    except Exception as e:
        logger.error(f"Scene video assembly error: {e}")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
