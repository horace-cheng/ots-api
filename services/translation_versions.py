"""
services/translation_versions.py

Async version history service for the API layer.
Uses core.storage (sync GCS) + core.database (async SQLAlchemy).
"""

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.storage import read_temp_json, write_temp_json, get_storage_client

logger = logging.getLogger(__name__)


def _version_path(order_id: str, version: int) -> str:
    return f"pipeline/{order_id}/versions/v{version}.json"


async def save_translation_version(
    db: AsyncSession,
    order_id: str,
    source: str,
    label: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any] | None:
    """Save current translations.json as a new version."""
    translations = read_temp_json(order_id, "translations.json")
    if not translations:
        logger.warning(f"No translations.json for order {order_id}, skipping version")
        return None

    result = await db.execute(text("""
        SELECT COALESCE(MAX(version), 0) + 1 AS next_ver
        FROM translation_versions
        WHERE order_id = :order_id
    """), {"order_id": order_id})
    row = result.fetchone()
    version = row.next_ver if row else 1

    gcs_path = _version_path(order_id, version)

    client = get_storage_client()
    bucket = client.bucket(settings.gcs_temp_bucket)
    blob = bucket.blob(gcs_path)
    blob.upload_from_string(
        json.dumps(translations, ensure_ascii=False, indent=2),
        content_type="application/json",
    )

    result = await db.execute(text("""
        INSERT INTO translation_versions
            (order_id, version, label, created_by, gcs_path, segment_count, source)
        VALUES
            (:order_id, :version, :label, :created_by, :gcs_path, :segment_count, :source)
        RETURNING id, version, label, created_by, created_at, gcs_path, segment_count, source
    """), {
        "order_id":      order_id,
        "version":       version,
        "label":         label,
        "created_by":    created_by,
        "gcs_path":      gcs_path,
        "segment_count": len(translations),
        "source":        source,
    })
    row = result.fetchone()
    await db.commit()

    logger.info(f"Version saved: order={order_id}, v{version}, source={source}")
    return dict(row._mapping) if row else None


def gcs_download_content(gcs_path: str) -> str:
    """Download file content from temp bucket."""
    client = get_storage_client()
    bucket = client.bucket(settings.gcs_temp_bucket)
    blob = bucket.blob(gcs_path)
    return blob.download_as_text(encoding="utf-8")


def gcs_upload_content(gcs_path: str, data: dict | list):
    """Upload file content to temp bucket."""
    client = get_storage_client()
    bucket = client.bucket(settings.gcs_temp_bucket)
    blob = bucket.blob(gcs_path)
    blob.upload_from_string(
        json.dumps(data, ensure_ascii=False, indent=2),
        content_type="application/json",
    )


async def list_versions(
    db: AsyncSession,
    order_id: str,
) -> list[dict[str, Any]]:
    """Return version metadata for an order, newest first."""
    rows = await db.execute(text("""
        SELECT tv.id, tv.version, tv.label, tv.source, tv.created_at,
               tv.segment_count, tv.gcs_path, u.email AS created_by_email
        FROM translation_versions tv
        LEFT JOIN users u ON u.id = tv.created_by
        WHERE tv.order_id = :order_id
        ORDER BY tv.version DESC
    """), {"order_id": order_id})
    return [dict(r._mapping) for r in rows.fetchall()]


async def restore_version(
    db: AsyncSession,
    order_id: str,
    version_id: str,
    restored_by: str | None = None,
) -> dict[str, Any] | None:
    """Restore translations.json from a version. Returns new version metadata."""
    result = await db.execute(text("""
        SELECT id, version, gcs_path, segment_count
        FROM translation_versions
        WHERE id = :vid AND order_id = :order_id
    """), {"vid": version_id, "order_id": order_id})
    row = result.fetchone()
    if not row:
        return None

    v = row._mapping
    content = gcs_download_content(v["gcs_path"])
    translations = json.loads(content)

    write_temp_json(order_id, "translations.json", translations)

    await db.execute(text("""
        UPDATE orders SET status = 'processing', delivered_at = NULL
        WHERE id = :order_id AND status = 'delivered'
    """), {"order_id": order_id})

    new_ver = await save_translation_version(
        db, order_id, source="restored",
        label=f"Restored from v{v['version']}",
        created_by=restored_by,
    )
    logger.info(f"Version restored: order={order_id}, from=v{v['version']}")
    return new_ver


async def diff_versions(
    db: AsyncSession,
    order_id: str,
    version_a_id: str,
    version_b_id: str,
) -> dict[str, Any]:
    """Diff two versions. Returns {changed, added, removed}."""
    result = await db.execute(text("""
        SELECT id, gcs_path FROM translation_versions
        WHERE id IN (:a, :b) AND order_id = :order_id
    """), {"a": version_a_id, "b": version_b_id, "order_id": order_id})
    rows = result.fetchall()

    paths = {str(r.id): r.gcs_path for r in rows}
    if version_a_id not in paths or version_b_id not in paths:
        raise ValueError("One or both versions not found")

    segs_a = {s["index"]: s for s in json.loads(gcs_download_content(paths[version_a_id]))}
    segs_b = {s["index"]: s for s in json.loads(gcs_download_content(paths[version_b_id]))}

    changed, added, removed = [], [], []
    for idx in sorted(set(segs_a) | set(segs_b)):
        a, b = segs_a.get(idx), segs_b.get(idx)
        if a is None and b:
            added.append({"index": idx, "source": b.get("source", ""), "text": b["translated"]})
        elif a and b is None:
            removed.append({"index": idx, "source": a.get("source", ""), "text": a["translated"]})
        elif a and b and a.get("translated") != b.get("translated"):
            changed.append({
                "index": idx, "source": a.get("source", ""),
                "old": a.get("translated", ""), "new": b.get("translated", ""),
            })

    return {"changed": changed, "added": added, "removed": removed}
