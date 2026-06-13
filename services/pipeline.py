"""
services/pipeline.py

觸發翻譯 Pipeline：把訂單 ID 發布到 Cloud Pub/Sub。
Cloud Workflows 訂閱後依 track_type 路由到對應的 Cloud Run Job。
"""

import json
import asyncio
import logging
from functools import lru_cache

from core.config import settings

logger = logging.getLogger(__name__)

# ── Job name mapping ──────────────────────────────────────────────────────────
DELIVER_JOB_NAMES = {
    "fast":      "ots-ft-deliver-{env}",
    "literary":  "ots-lt-deliver-{env}",
    "gutenberg": "ots-gt-deliver-{env}",
}


# ── Stage-level rerun mapping ─────────────────────────────────────────────────
# Used by POST /admin/orders/{id}/rerun-stage. Maps an admin-facing
# `stage` name to the Cloud Run Job that handles it, plus the per-stage
# extra env vars (e.g. REDELIVER for deliver).
RERUN_STAGE_JOBS = {
    "fetcher":         ("ots-gt-fetcher-{env}",         {}),
    "chapter_splitter":("ots-gt-chapter-splitter-{env}",{}),
    "extract_terms":   ("ots-gt-extract-terms-{env}",   {}),
    "translate":       ("ots-gt-translate-{env}",       {}),
    "simplify":        ("ots-gt-simplify-{env}",        {}),
    "tailo":           ("ots-gt-tailo-{env}",           {}),
    "deliver":         ("ots-gt-deliver-{env}",         {"REDELIVER": "true"}),
}

# Ordered list for `stage="all"` — runs the whole pipeline from scratch.
RERUN_STAGE_ORDER = [
    "fetcher", "chapter_splitter", "extract_terms", "translate",
    "simplify", "tailo", "deliver",
]


@lru_cache(maxsize=1)
def _get_publisher():
    """Pub/Sub PublisherClient 單例"""
    from google.cloud import pubsub_v1
    return pubsub_v1.PublisherClient()


async def trigger_pipeline(order_id: str) -> str:
    """
    將訂單 ID 發布到 Pub/Sub topic，觸發 Pipeline。
    回傳 message_id。
    失敗時記 log 但不拋出例外（付款已成功，pipeline 失敗可重試）。
    """
    try:
        publisher  = _get_publisher()
        topic_path = publisher.topic_path(
            settings.project_id,
            settings.pubsub_topic,
        )
        message = json.dumps({
            "order_id": order_id,
            "source":   "payment_confirmed",
        }).encode("utf-8")

        # PublisherClient.publish() 是同步的，用 run_in_executor 避免 block event loop
        loop = asyncio.get_event_loop()
        future = await loop.run_in_executor(
            None,
            lambda: publisher.publish(topic_path, message)
        )
        message_id = future.result()

        logger.info(f"Pipeline triggered: order={order_id}, message_id={message_id}")
        return message_id

    except Exception as e:
        logger.error(f"Failed to trigger pipeline for order {order_id}: {e}")
        # TODO: 寫入 dead-letter queue 或 Cloud Tasks 做延遲重試
        return ""


VIDEO_PREP_JOB_NAME = "ots-gt-video-prep-{env}"


async def trigger_video_prep_job(order_id: str) -> str:
    """Trigger gt_video_prep (storyboarding) Cloud Run Job."""
    try:
        from google.cloud.run_v2 import JobsClient
        from google.cloud.run_v2.types import RunJobRequest, EnvVar

        env = settings.env
        project_id = settings.project_id
        region = settings.region
        full_job_name = VIDEO_PREP_JOB_NAME.format(env=env)
        name = f"projects/{project_id}/locations/{region}/jobs/{full_job_name}"

        request = RunJobRequest(
            name=name,
            overrides=RunJobRequest.Overrides(
                container_overrides=[
                    RunJobRequest.Overrides.ContainerOverride(
                        env=[
                            EnvVar(name="ORDER_ID", value=order_id),
                        ]
                    )
                ]
            ),
        )

        loop = asyncio.get_event_loop()
        client = JobsClient()
        operation = await loop.run_in_executor(
            None, lambda: client.run_job(request=request)
        )
        logger.info(f"Video prep job triggered: order={order_id}, job={full_job_name}")
        return order_id

    except Exception as e:
        logger.error(f"Failed to trigger video prep job for order {order_id}: {e}")
        raise


async def trigger_deliver_job(order_id: str, track_type: str) -> str:
    """
    直接觸發 Cloud Run Jobs 的 deliver job（不跑完整 pipeline）。
    用於只重新產出交付檔案（不重新翻譯）。
    """
    job_name = DELIVER_JOB_NAMES.get(track_type)
    if not job_name:
        raise ValueError(f"Unknown track type: {track_type}")

    try:
        import asyncio
        from google.cloud.run_v2 import JobsClient
        from google.cloud.run_v2.types import RunJobRequest, EnvVar

        env = settings.env
        project_id = settings.project_id
        region = settings.region
        full_job_name = job_name.format(env=env)
        name = f"projects/{project_id}/locations/{region}/jobs/{full_job_name}"

        request = RunJobRequest(
            name=name,
            overrides=RunJobRequest.Overrides(
                container_overrides=[
                    RunJobRequest.Overrides.ContainerOverride(
                        env=[
                            EnvVar(name="ORDER_ID", value=order_id),
                            EnvVar(name="REDELIVER", value="true"),
                        ]
                    )
                ]
            ),
        )

        loop = asyncio.get_event_loop()
        client = JobsClient()
        operation = await loop.run_in_executor(
            None, lambda: client.run_job(request=request)
        )
        # Fire-and-forget: don't call operation.result() — that polls for
        # completion and requires run.operations.get permission. The job is
        # already submitted.
        logger.info(f"Deliver job triggered: order={order_id}, job={full_job_name}")
        return order_id

    except Exception as e:
        logger.error(f"Failed to trigger deliver job for order {order_id}: {e}")
        raise


async def trigger_rerun_stage(order_id: str, stage: str) -> str:
    """
    Trigger a single stage (or all stages in order) of the Gutenberg
    pipeline as a Cloud Run Job execution. Used by
    POST /admin/orders/{id}/rerun-stage to let admins recover from a
    partial pipeline failure without re-running the whole workflow.

    `stage` is one of: fetcher, chapter_splitter, extract_terms,
    translate, simplify, tailo, deliver, all.

    Returns the (formatted) job name of the last triggered stage, or
    comma-separated names for stage="all".
    """
    if stage == "all":
        triggered: list[str] = []
        for s in RERUN_STAGE_ORDER:
            job_name, extra_env = RERUN_STAGE_JOBS[s]
            await _run_cloud_run_job(order_id, job_name, extra_env)
            triggered.append(job_name.format(env=settings.env))
        return ",".join(triggered)

    if stage not in RERUN_STAGE_JOBS:
        raise ValueError(
            f"Unknown stage: {stage!r}. "
            f"Must be one of: {', '.join(list(RERUN_STAGE_JOBS) + ['all'])}"
        )

    job_name, extra_env = RERUN_STAGE_JOBS[stage]
    await _run_cloud_run_job(order_id, job_name, extra_env)
    return job_name.format(env=settings.env)


async def _run_cloud_run_job(order_id: str, job_template: str, extra_env: dict) -> None:
    """Fire-and-forget trigger of a Cloud Run Job for one stage.

    Errors are raised so the caller can surface them to the admin.
    """
    try:
        from google.cloud.run_v2 import JobsClient
        from google.cloud.run_v2.types import RunJobRequest, EnvVar

        full_job_name = job_template.format(env=settings.env)
        name = (
            f"projects/{settings.project_id}/locations/"
            f"{settings.region}/jobs/{full_job_name}"
        )

        env_vars = [EnvVar(name="ORDER_ID", value=order_id)]
        for k, v in extra_env.items():
            env_vars.append(EnvVar(name=k, value=v))

        request = RunJobRequest(
            name=name,
            overrides=RunJobRequest.Overrides(
                container_overrides=[
                    RunJobRequest.Overrides.ContainerOverride(env=env_vars)
                ]
            ),
        )

        loop = asyncio.get_event_loop()
        client = JobsClient()
        await loop.run_in_executor(
            None, lambda: client.run_job(request=request)
        )
        logger.info(
            f"Rerun stage triggered: order={order_id}, job={full_job_name}, "
            f"extra_env={list(extra_env)}"
        )

    except Exception as e:
        logger.error(
            f"Failed to trigger rerun stage for order {order_id}: {e}"
        )
        raise


async def trigger_pipeline_retry(order_id: str, delay_seconds: int = 60):
    """
    延遲重試（用於 webhook 確認後 pipeline 失敗的補償）。
    TODO: 改用 Cloud Tasks 實作更可靠的延遲重試。
    """
    await asyncio.sleep(delay_seconds)
    await trigger_pipeline(order_id)
