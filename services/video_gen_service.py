"""services/video_gen_service.py — Scene-level TTS, image gen, and video assembly.

Used by the admin video-storyboard editing page for per-scene previews.
"""
import base64
import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import time
import wave
from typing import Optional
from urllib.parse import urljoin

import requests

from core.config import settings

logger = logging.getLogger(__name__)

# ── Target image dimensions (1280×720 = 720p, 16:9, YouTube-friendly) ──────────
IMG_W, IMG_H = 1280, 720


# ── Helper: resize + center-crop any image to target dimensions ────────────────

def _resize_to_720p(image_bytes: bytes) -> bytes:
    """Scale and center-crop image bytes to 1280×720, return JPEG bytes."""
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(image_bytes))
    if img.size == (IMG_W, IMG_H):
        return image_bytes
    # Center-crop to 16:9 then scale
    src_w, src_h = img.size
    target_ratio = IMG_W / IMG_H
    src_ratio = src_w / src_h
    if src_ratio > target_ratio:
        # Image is wider — crop width
        new_w = int(src_h * target_ratio)
        offset = (src_w - new_w) // 2
        img = img.crop((offset, 0, offset + new_w, src_h))
    elif src_ratio < target_ratio:
        # Image is taller — crop height
        new_h = int(src_w / target_ratio)
        offset = (src_h - new_h) // 2
        img = img.crop((0, offset, src_w, offset + new_h))
    img = img.resize((IMG_W, IMG_H), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


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
                   speaking_rate: float = 1.0,
                   language_code: str = "cmn-TW",
                   short_pause_duration: int = 150,
                   long_pause_duration: int = 450) -> bytes:
        self._ensure_token()
        url = urljoin(self.base_url, "/api/v1/tts/synthesize")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        payload = {
            "input": {"text": text, "textType": "characters"},
            "voice": {"model": "broncitts", "languageCode": language_code, "name": voice_id},
            "audioConfig": {
                "speakingRate": speaking_rate,
                "sampleRate": 16000,
                "shortPauseDuration": short_pause_duration,
                "longPauseDuration": long_pause_duration,
            },
            "outputConfig": {"streamMode": 0},
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        return resp.content


def synthesize_speech(text: str, voice_id: str = "cmn-TW-vs2-F04",
                      speaking_rate: float = 1.0,
                      language_code: str = "cmn-TW",
                      short_pause_duration: int = 150,
                      long_pause_duration: int = 450) -> bytes:
    """Synthesize text to WAV bytes via BRONCI TTS."""
    client = BronciTTSClient(
        settings.bronci_username,
        settings.bronci_password,
        settings.bronci_base_url,
    )
    return client.synthesize(text, voice_id=voice_id, speaking_rate=speaking_rate,
                             language_code=language_code,
                             short_pause_duration=short_pause_duration,
                             long_pause_duration=long_pause_duration)


# ── NVIDIA build.nvidia.com Image Generation ──────────────────────────────────

_NVIDIA_FLUX_URL = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-schnell"


def _nvidia_image(prompt: str) -> bytes:
    """Generate image via NVIDIA build.nvidia.com NIM API with FLUX model."""
    token = settings.nvidia_api_token
    if not token:
        raise ValueError("NVIDIA API token not configured")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        "mode": "base",
        "seed": random.randint(0, 2**31 - 1),
        "steps": 4,
        "width": IMG_W,
        "height": 768,  # NVIDIA requires 768/832/896/960/1024/1088/1152/1216/1280/1344
    }
    resp = requests.post(_NVIDIA_FLUX_URL, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    artifacts = data.get("artifacts", [])
    if not artifacts:
        raise ValueError(f"NVIDIA no artifacts in response: {data}")
    finish = artifacts[0].get("finishReason", "")
    if finish != "SUCCESS":
        raise ValueError(f"NVIDIA finishReason={finish} — content may be filtered")
    b64 = artifacts[0].get("base64")
    if not b64:
        raise ValueError(f"NVIDIA artifact missing base64: {artifacts[0]}")
    img = base64.b64decode(b64)
    img = _resize_to_720p(img)
    seed = artifacts[0].get("seed", "?")
    logger.info(f"NVIDIA image gen succeeded — size={len(img)}B seed={seed} finish={finish} prompt={prompt[:60]}")
    return img


# ── Hugging Face Image Generation ──────────────────────────────────────────────

def _hf_image(prompt: str) -> bytes:
    """Generate image via Hugging Face Inference API."""
    url = f"https://router.huggingface.co/hf-inference/models/{settings.hf_image_model}"
    headers = {"Authorization": f"Bearer {settings.hf_api_token}"}
    payload = {
        "inputs": prompt,
        "parameters": {
            "negative_prompt": "blurry, low quality, distorted, text, watermark, signature",
            "width": IMG_W,
            "height": IMG_H,
            "num_inference_steps": 4,
        },
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    if "image" not in content_type:
        raise ValueError(f"HF unexpected response type: {content_type}")
    img = resp.content
    img = _resize_to_720p(img)
    logger.info(f"HF image gen succeeded — size={len(img)}B type={content_type} model={settings.hf_image_model} prompt={prompt[:60]}")
    return img


_REPLICATE_API = "https://api.replicate.com/v1/models"
_REPLICATE_POLL = "https://api.replicate.com/v1/predictions"


def _replicate_image(prompt: str) -> bytes:
    """Generate image via Replicate API with FLUX model."""
    token = settings.replicate_api_token
    if not token:
        raise ValueError("Replicate API token not configured")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "input": {
            "prompt": prompt,
            "num_outputs": 1,
            "aspect_ratio": "16:9",
            "output_format": "jpg",
            "go_fast": True,
        }
    }
    r = requests.post(
        f"{_REPLICATE_API}/black-forest-labs/flux-schnell/predictions",
        json=payload, headers=headers, timeout=30,
    )
    r.raise_for_status()
    pred = r.json()
    pred_id = pred.get("id", "?")
    poll_url = pred.get("urls", {}).get("get", f"{_REPLICATE_POLL}/{pred_id}")
    for _ in range(60):
        time.sleep(2)
        pr = requests.get(poll_url, headers=headers, timeout=30)
        pr.raise_for_status()
        pr_data = pr.json()
        status = pr_data.get("status")
        if status == "succeeded":
            output = pr_data.get("output")
            if isinstance(output, list) and output:
                img_r = requests.get(output[0], timeout=60)
                img_r.raise_for_status()
                img = img_r.content
                img = _resize_to_720p(img)
                model_ver = pr_data.get("version", "?")
                logger.info(f"Replicate image gen succeeded — size={len(img)}B pred_id={pred_id} model_version={model_ver} prompt={prompt[:60]}")
                return img
            raise ValueError(f"Replicate no output: {output}")
        if status == "failed":
            raise ValueError(f"Replicate prediction failed: {pr_data.get('error', 'unknown')}")
    raise TimeoutError("Replicate prediction timed out")


def generate_image(prompt: str) -> bytes:
    """Generate image — tries Hugging Face first, then NVIDIA, then Replicate."""
    if settings.hf_api_token:
        try:
            return _hf_image(prompt)
        except Exception as e:
            logger.warning(f"HF image gen failed ({e})")
    if settings.nvidia_api_token:
        try:
            return _nvidia_image(prompt)
        except Exception as e:
            logger.warning(f"NVIDIA image gen failed ({e}), falling back to Replicate")
    return _replicate_image(prompt)


# ── Scene Video Assembly ───────────────────────────────────────────────────────

def _run_ffmpeg(args: list[str], desc: str = ""):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"] + args
    logger.info(f"FFmpeg: {desc}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr}")


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

        with wave.open(wav_path, 'r') as wf:
            audio_duration = wf.getnframes() / wf.getframerate()

        result = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
             "-loop", "1", "-i", img_path, "-i", wav_path,
             "-c:v", "libx264", "-tune", "stillimage",
             "-b:v", "2M",
             "-c:a", "aac", "-b:a", "192k",
             "-pix_fmt", "yuv420p",
             "-t", f"{audio_duration:.3f}",
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


# ── Chapter Video Assembly ──────────────────────────────────────────────────────

_CJK_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSerifCJKtc-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.otf",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
]

def _format_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def assemble_chapter_video(
    order_id: str,
    chapter_index: int,
    scenes: list[dict],
    title: str = "",
    language: str = "zh",
) -> tuple[Optional[bytes], str]:
    """Assemble all scenes in a chapter into one MP4 + SRT subtitle file.

    Downloads audio + image for each scene from GCS temp bucket,
    creates clips, concatenates into MP4, and generates an SRT with
    narration text timed to each scene.

    The `language` param selects which narration track to use (e.g. 'zh'
    or 'tai-lo') for both SRT text and audio asset paths.
    """
    from google.cloud import storage
    from PIL import Image, ImageDraw, ImageFont

    client = storage.Client(project=settings.project_id)
    bucket = client.bucket(settings.gcs_temp_bucket)

    tmpdir = tempfile.mkdtemp(prefix=f"ch{chapter_index}_")
    try:
        concat_file = os.path.join(tmpdir, "concat.txt")
        clip_index = 0
        total_scenes = 0
        srt_index = 1
        cumulative_time = 0.0
        srt_lines: list[str] = []

        with open(concat_file, "w") as cf:
            # ── Title card ──
            if title:
                W, H = IMG_W, IMG_H
                img = Image.new("RGB", (W, H), (26, 26, 46))
                draw = ImageDraw.Draw(img)
                font_cjk = None
                for p in _CJK_FONTS:
                    if os.path.exists(p):
                        font_cjk = ImageFont.truetype(p, 52)
                        break
                font_en = None
                for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                          "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
                    if os.path.exists(p):
                        font_en = ImageFont.truetype(p, 22)
                        break
                if font_cjk is None:
                    font_cjk = ImageFont.load_default()
                if font_en is None:
                    font_en = ImageFont.load_default()
                zh = title
                _, _, tw, _ = draw.textbbox((0, 0), f"Chapter {chapter_index + 1}", font=font_en)
                draw.text(((W - tw) / 2, H * 0.32), f"Chapter {chapter_index + 1}", fill=(180, 180, 200), font=font_en)
                _, _, tw, _ = draw.textbbox((0, 0), zh, font=font_cjk)
                draw.text(((W - tw) / 2, H * 0.45), zh, fill=(255, 255, 255), font=font_cjk)
                title_png = os.path.join(tmpdir, "title.png")
                img.save(title_png)
                title_mp4 = os.path.join(tmpdir, "title.mp4")
                _run_ffmpeg([
                    "-loop", "1", "-i", title_png,
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=16000",
                    "-c:v", "libx264", "-tune", "stillimage", "-b:v", "2M",
                    "-c:a", "aac", "-b:a", "192k", "-shortest",
                    "-pix_fmt", "yuv420p", "-t", "4",
                    title_mp4,
                ], desc=f"Title card ch{chapter_index + 1}")
                cf.write(f"file '{title_mp4}'\n")
                clip_index += 1
                cumulative_time += 4

            # ── Scene clips ──
            for scene in sorted(scenes, key=lambda s: s["scene_index"]):
                s_idx = scene["scene_index"]
                wav_blob = bucket.blob(f"pipeline/{order_id}/scenes/{chapter_index}_{s_idx}/{language}/narration.wav")
                jpg_blob = bucket.blob(f"pipeline/{order_id}/scenes/{chapter_index}_{s_idx}/visual.jpg")
                if not wav_blob.exists() or not jpg_blob.exists():
                    continue

                wav_path = os.path.join(tmpdir, f"s{chapter_index}_{s_idx}.wav")
                jpg_path = os.path.join(tmpdir, f"s{chapter_index}_{s_idx}.jpg")
                clip_path = os.path.join(tmpdir, f"clip_{clip_index}.mp4")
                wav_blob.download_to_filename(wav_path)
                jpg_blob.download_to_filename(jpg_path)

                with wave.open(wav_path, 'r') as wf:
                    audio_duration = wf.getnframes() / wf.getframerate()

                _run_ffmpeg([
                    "-loop", "1", "-i", jpg_path, "-i", wav_path,
                    "-c:v", "libx264", "-tune", "stillimage", "-b:v", "2M",
                    "-c:a", "aac", "-b:a", "192k",
                    "-pix_fmt", "yuv420p",
                    "-t", f"{audio_duration:.3f}",
                    clip_path,
                ], desc=f"Scene {chapter_index}.{s_idx}")
                cf.write(f"file '{clip_path}'\n")
                clip_index += 1
                total_scenes += 1

                # SRT entry — read narration from the correct track
                tracks = scene.get("tracks", {})
                if language in tracks:
                    narration = tracks[language].get("narration_text", "").strip()
                else:
                    narration = scene.get("narration_text", "").strip()
                if narration:
                    start_time = cumulative_time
                    end_time = cumulative_time + audio_duration
                    srt_lines.append(f"{srt_index}\n")
                    srt_lines.append(f"{_format_srt_time(start_time)} --> {_format_srt_time(end_time)}\n")
                    srt_lines.append(f"{narration}\n\n")
                    srt_index += 1

                cumulative_time += audio_duration

        if total_scenes == 0:
            logger.warning(f"No scenes with assets for chapter {chapter_index}")
            return None, ""

        output_path = os.path.join(tmpdir, f"chapter_{chapter_index:02d}.mp4")
        _run_ffmpeg([
            "-f", "concat", "-safe", "0",
            "-fflags", "+genpts",
            "-i", concat_file,
            "-c", "copy", "-movflags", "+faststart",
            output_path,
        ], desc=f"Chapter {chapter_index} ({total_scenes} scenes)")

        with open(output_path, "rb") as f:
            mp4_bytes = f.read()

        srt_content = "".join(srt_lines)
        return mp4_bytes, srt_content
    except Exception as e:
        logger.error(f"Chapter video assembly error: {e}")
        return None, ""
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── PixVerse V6 on fal.ai ───────────────────────────────────────────────────

class FalPixVerseClient:
    """Client for PixVerse V6 text-to-video via fal.ai."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._base = "https://fal.run/fal-ai/pixverse/v6/text-to-video"

    def generate(self, prompt: str, duration_sec: float) -> Optional[bytes]:
        """Generate a video via PixVerse V6. Returns MP4 bytes or None."""
        duration = min(int(duration_sec) or 1, 15)
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "prompt": prompt,
            "aspect_ratio": "16:9",
            "duration": duration,
            "fps": 24,
            "resolution": "720p",
            "generate_audio_switch": False,
        }
        resp = requests.post(self._base, json=payload, headers=headers, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        video_url = data.get("video", {}).get("url")
        if not video_url:
            logger.error(f"PixVerse no video URL: {data}")
            return None
        logger.info(f"PixVerse gen OK — duration_requested={duration}s prompt={prompt[:60]}")
        vr = requests.get(video_url, timeout=120)
        vr.raise_for_status()
        return vr.content


# ── LTX 2.3 Fast on fal.ai ────────────────────────────────────────────────────

class FalLtxClient:
    """Client for LTX 2.3 Fast text-to-video via fal.ai."""

    VALID_DURATIONS = [6, 8, 10, 12, 14, 16, 18, 20]

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._base = "https://fal.run/fal-ai/ltx-2.3/text-to-video/fast"

    def _round_duration(self, seconds: float) -> int:
        for d in self.VALID_DURATIONS:
            if d >= seconds:
                return d
        return self.VALID_DURATIONS[-1]

    def generate(self, prompt: str, duration_sec: float,
                 aspect_ratio: str = "16:9") -> Optional[bytes]:
        """Generate a video via LTX 2.3 Fast. Returns MP4 bytes or None."""
        duration = self._round_duration(duration_sec)
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "prompt": prompt,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
            "resolution": "1080p",
        }
        resp = requests.post(self._base, json=payload, headers=headers, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        video_url = data.get("video", {}).get("url")
        if not video_url:
            logger.error(f"LTX no video URL: {data}")
            return None
        logger.info(f"LTX gen OK — duration_requested={duration}s prompt={prompt[:60]}")
        vr = requests.get(video_url, timeout=120)
        vr.raise_for_status()
        return vr.content


# ── Scene Video Assembly (Audio Overlay) ──────────────────────────────────────

def assemble_scene_video_from_clip(video_bytes: bytes, audio_bytes: bytes) -> Optional[bytes]:
    """Overlay TTS audio onto an LTX-generated video clip.
    
    Replaces the video's audio track with the TTS audio.
    Trims to the shorter of video/audio duration.
    Returns MP4 bytes or None on failure.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("FFmpeg not available for scene video assembly")
        return None

    tmpdir = tempfile.mkdtemp(prefix="scene_overlay_")
    try:
        vid_path = os.path.join(tmpdir, "input.mp4")
        wav_path = os.path.join(tmpdir, "input.wav")
        out_path = os.path.join(tmpdir, "output.mp4")
        with open(vid_path, "wb") as f:
            f.write(video_bytes)
        with open(wav_path, "wb") as f:
            f.write(audio_bytes)

        result = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
             "-i", vid_path, "-i", wav_path,
             "-c:v", "copy",
             "-map", "0:v:0",
             "-map", "1:a:0",
             "-shortest",
             "-movflags", "+faststart",
             out_path],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            logger.error(f"Scene overlay failed: {result.stderr}")
            return None

        with open(out_path, "rb") as f:
            return f.read()
    except Exception as e:
        logger.error(f"Scene overlay error: {e}")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Chapter Merge (Concatenate Scene Videos) ──────────────────────────────────

def merge_chapter_videos(
    order_id: str,
    chapter_index: int,
    scenes: list[dict],
    language: str = "zh",
    title: str = "",
) -> tuple[Optional[bytes], str]:
    """Concatenate all scene videos in a chapter into one MP4 + SRT.
    
    Downloads each scene's video + audio from GCS, concatenates with
    FFmpeg, and generates an SRT with narration text timed to each scene.
    Returns (mp4_bytes, srt_content) or (None, "") on failure.
    """
    from google.cloud import storage
    from PIL import Image, ImageDraw, ImageFont

    client = storage.Client(project=settings.project_id)
    bucket = client.bucket(settings.gcs_temp_bucket)
    out_bucket = client.bucket(settings.gcs_outputs_bucket)

    tmpdir = tempfile.mkdtemp(prefix=f"ch{chapter_index}_merge_")
    try:
        concat_file = os.path.join(tmpdir, "concat.txt")
        clip_index = 0
        total_scenes = 0
        srt_index = 1
        cumulative_time = 0.0
        srt_lines: list[str] = []

        with open(concat_file, "w") as cf:
            # ── Title card ──
            if title:
                W, H = IMG_W, IMG_H
                img = Image.new("RGB", (W, H), (26, 26, 46))
                draw = ImageDraw.Draw(img)
                font_cjk = None
                for p in _CJK_FONTS:
                    if os.path.exists(p):
                        font_cjk = ImageFont.truetype(p, 52)
                        break
                font_en = None
                for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                          "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
                    if os.path.exists(p):
                        font_en = ImageFont.truetype(p, 22)
                        break
                if font_cjk is None:
                    font_cjk = ImageFont.load_default()
                if font_en is None:
                    font_en = ImageFont.load_default()
                zh = title
                _, _, tw, _ = draw.textbbox((0, 0), f"Chapter {chapter_index + 1}", font=font_en)
                draw.text(((W - tw) / 2, H * 0.32), f"Chapter {chapter_index + 1}", fill=(180, 180, 200), font=font_en)
                _, _, tw, _ = draw.textbbox((0, 0), zh, font=font_cjk)
                draw.text(((W - tw) / 2, H * 0.45), zh, fill=(255, 255, 255), font=font_cjk)
                title_png = os.path.join(tmpdir, "title.png")
                img.save(title_png)
                title_mp4 = os.path.join(tmpdir, "title.mp4")
                _run_ffmpeg([
                    "-loop", "1", "-i", title_png,
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=16000",
                    "-c:v", "libx264", "-tune", "stillimage", "-b:v", "2M",
                    "-c:a", "aac", "-b:a", "192k", "-shortest",
                    "-pix_fmt", "yuv420p", "-t", "4",
                    title_mp4,
                ], desc=f"Title card ch{chapter_index + 1}")
                cf.write(f"file '{title_mp4}'\n")
                clip_index += 1
                cumulative_time += 4

            # ── Scene clips ──
            for scene in sorted(scenes, key=lambda s: s["scene_index"]):
                s_idx = scene["scene_index"]
                # Try scene video first, fall back to image+audio
                vid_blob = bucket.blob(
                    f"pipeline/{order_id}/scenes/{chapter_index}_{s_idx}/{language}/scene_video.mp4"
                )
                if vid_blob.exists():
                    vid_path = os.path.join(tmpdir, f"s{chapter_index}_{s_idx}.mp4")
                    vid_blob.download_to_filename(vid_path)
                    cf.write(f"file '{vid_path}'\n")
                    clip_index += 1
                    total_scenes += 1

                    # Probe duration
                    probe = subprocess.run(
                        [shutil.which("ffprobe") or "ffprobe",
                         "-v", "error", "-show_entries", "format=duration",
                         "-of", "csv=p=0", vid_path],
                        capture_output=True, text=True, timeout=30,
                    )
                    scene_dur = float(probe.stdout.strip()) if probe.returncode == 0 else 0
                else:
                    # Fallback: image + audio loop
                    wav_blob = bucket.blob(
                        f"pipeline/{order_id}/scenes/{chapter_index}_{s_idx}/{language}/narration.wav"
                    )
                    jpg_blob = bucket.blob(
                        f"pipeline/{order_id}/scenes/{chapter_index}_{s_idx}/visual.jpg"
                    )
                    if not wav_blob.exists() or not jpg_blob.exists():
                        continue
                    wav_path = os.path.join(tmpdir, f"s{chapter_index}_{s_idx}.wav")
                    jpg_path = os.path.join(tmpdir, f"s{chapter_index}_{s_idx}.jpg")
                    clip_path = os.path.join(tmpdir, f"clip_{clip_index}.mp4")
                    wav_blob.download_to_filename(wav_path)
                    jpg_blob.download_to_filename(jpg_path)
                    with wave.open(wav_path, 'r') as wf:
                        scene_dur = wf.getnframes() / wf.getframerate()
                    _run_ffmpeg([
                        "-loop", "1", "-i", jpg_path, "-i", wav_path,
                        "-c:v", "libx264", "-tune", "stillimage", "-b:v", "2M",
                        "-c:a", "aac", "-b:a", "192k",
                        "-pix_fmt", "yuv420p",
                        "-t", f"{scene_dur:.3f}",
                        clip_path,
                    ], desc=f"Scene {chapter_index}.{s_idx} (image fallback)")
                    cf.write(f"file '{clip_path}'\n")
                    clip_index += 1
                    total_scenes += 1

                # SRT entry
                tracks = scene.get("tracks", {})
                if language in tracks:
                    narration = tracks[language].get("narration_text", "").strip()
                else:
                    narration = scene.get("narration_text", "").strip()
                if narration and scene_dur > 0:
                    srt_lines.append(f"{srt_index}\n")
                    srt_lines.append(f"{_format_srt_time(cumulative_time)} --> {_format_srt_time(cumulative_time + scene_dur)}\n")
                    srt_lines.append(f"{narration}\n\n")
                    srt_index += 1
                    cumulative_time += scene_dur

        if total_scenes == 0:
            logger.warning(f"No scenes with assets for chapter {chapter_index}")
            return None, ""

        output_path = os.path.join(tmpdir, f"chapter_{chapter_index:02d}.mp4")
        _run_ffmpeg([
            "-f", "concat", "-safe", "0",
            "-fflags", "+genpts",
            "-i", concat_file,
            "-c", "copy", "-movflags", "+faststart",
            output_path,
        ], desc=f"Chapter {chapter_index} merge ({total_scenes} videos)")

        with open(output_path, "rb") as f:
            mp4_bytes = f.read()

        srt_content = "".join(srt_lines)
        return mp4_bytes, srt_content
    except Exception as e:
        logger.error(f"Chapter merge error: {e}")
        return None, ""
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
