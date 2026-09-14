import os
import sys
import uuid
import time
import datetime
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from sqlalchemy.orm import Session

from core.config import settings
from database.database import (
    SessionLocal,
    get_review_job,
    update_review_job_status,
    save_findings,
    increment_repo_file_counters,
    init_db,
)
from services.llm_service import BaseLLMService, get_llm_service
from services.rabbitmq import RabbitMQService

worker_name = os.getenv("WORKER_ID", f"worker-{uuid.uuid4().hex[:6]}")
logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [%(levelname)s] [{worker_name}] %(message)s",
)
logger = logging.getLogger(__name__)


def process_review_job(
    payload: Dict[str, Any],
    db: Session,
    llm_service: BaseLLMService,
) -> int:
    """Processes a single source file review job:

    1. Updates ReviewJob status in DB to 'processing'.
    2. Reads the file content from local disk.
    3. Invokes LLM service for code review findings.
    4. Persists findings in the DB.
    5. Updates ReviewJob status to 'completed'.
    6. Increments repository completed_files counter.
    Returns the count of findings discovered.
    """
    job_id = payload.get("job_id")
    repository_id = payload.get("repository_id")
    file_path = payload.get("file_path")
    language = payload.get("language", "unknown")
    attempt = payload.get("attempt", 1)

    if not job_id or not repository_id or not file_path:
        raise ValueError(f"Invalid review job payload: {payload}")

    logger.info(f"Analyzing file '{file_path}' (lang={language}, attempt={attempt}) for job '{job_id}'")

    # 1. Update review job status to 'processing'
    now_utc = datetime.datetime.utcnow()
    update_review_job_status(
        db,
        job_id=job_id,
        status="processing",
        started_at=now_utc,
        increment_attempts=True,
    )

    # 2. Locate source file on disk
    file_disk_path = settings.extracted_dir / repository_id / file_path
    if not file_disk_path.exists():
        err_msg = f"File not found on disk: {file_disk_path}"
        logger.error(err_msg)
        update_review_job_status(db, job_id=job_id, status="failed", error_message=err_msg)
        increment_repo_file_counters(db, repository_id=repository_id, failed_inc=1)
        raise FileNotFoundError(err_msg)

    # 3. Read code content safely
    try:
        with open(file_disk_path, "r", encoding="utf-8", errors="replace") as f:
            code_content = f.read()
    except Exception as e:
        err_msg = f"Failed to read file {file_disk_path}: {e}"
        logger.error(err_msg)
        update_review_job_status(db, job_id=job_id, status="failed", error_message=err_msg)
        increment_repo_file_counters(db, repository_id=repository_id, failed_inc=1)
        raise

    # 4. Invoke LLM service
    try:
        findings = llm_service.analyze_code(code_content, language=language, file_path=file_path)
    except Exception as e:
        err_msg = f"LLM analysis failed for {file_path}: {e}"
        logger.error(err_msg)
        update_review_job_status(db, job_id=job_id, status="failed", error_message=err_msg)
        increment_repo_file_counters(db, repository_id=repository_id, failed_inc=1)
        raise

    # 5. Save findings in database
    if findings:
        save_findings(db, review_job_id=job_id, file_path=file_path, findings=findings)

    # 6. Update review job status to 'completed'
    update_review_job_status(
        db,
        job_id=job_id,
        status="completed",
        completed_at=datetime.datetime.utcnow(),
    )

    # 7. Update repository progress
    increment_repo_file_counters(db, repository_id=repository_id, completed_inc=1)

    logger.info(f"Completed analysis of '{file_path}': {len(findings)} issue(s) recorded.")
    return len(findings)


def run_worker():
    """Main consumer loop for review workers."""
    logger.info(f"Starting Review Worker ({worker_name})...")
    init_db()
    queue_service = RabbitMQService()
    llm_service = get_llm_service()

    def message_handler(payload: Dict[str, Any], channel, delivery_tag):
        db = SessionLocal()
        try:
            process_review_job(payload, db, llm_service)
            channel.basic_ack(delivery_tag=delivery_tag)
            logger.info(f"Job {payload.get('job_id')} acknowledged by {worker_name}.")
        except Exception as e:
            logger.error(f"Error processing review job {payload.get('job_id')}: {e}")
            # Failed jobs retry up to 3 times before being routed to DLQ
            attempt = payload.get("attempt", 1)
            if attempt < 3:
                logger.warning(f"Re-queuing job {payload.get('job_id')} (attempt {attempt + 1}/3)...")
                payload["attempt"] = attempt + 1
                channel.basic_ack(delivery_tag=delivery_tag)
                queue_service.publish_review_job(
                    job_id=payload["job_id"],
                    repository_id=payload["repository_id"],
                    file_path=payload["file_path"],
                    language=payload.get("language", "unknown"),
                    attempt=attempt + 1,
                )
            else:
                logger.error(f"Max retries reached for job {payload.get('job_id')}. Routing to DLQ.")
                channel.basic_nack(delivery_tag=delivery_tag, requeue=False)
        finally:
            db.close()

    queue_service.consume(
        queue_name=settings.RABBITMQ_REVIEW_QUEUE,
        callback=message_handler,
        prefetch_count=1,
    )


if __name__ == "__main__":
    run_worker()
