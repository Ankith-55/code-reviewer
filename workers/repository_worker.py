import os
import sys
import uuid
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from sqlalchemy.orm import Session

from core.config import settings
from database.database import (
    SessionLocal,
    get_repository,
    create_repository,
    update_repository,
    create_review_job,
    init_db,
)
from services.repository_service import SafeExtractor, RepositoryScanner, RepositoryServiceError
from services.rabbitmq import RabbitMQService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [RepositoryWorker] %(message)s",
)
logger = logging.getLogger(__name__)


def process_repository_job(
    payload: Dict[str, Any],
    db: Session,
    queue_service: Any,
) -> int:
    """Processes an incoming repository extraction and file scanning job.

    1. Updates repository status in DB to 'processing'.
    2. Safely extracts ZIP archive into local extracted storage.
    3. Recursively scans directory for valid source code files.
    4. Creates review jobs in DB.
    5. Publishes review jobs to the review queue.
    Returns the number of review jobs dispatched.
    """
    repository_id = payload.get("repository_id")
    zip_path_str = payload.get("zip_path")

    if not repository_id or not zip_path_str:
        raise ValueError(f"Invalid repository job payload: {payload}")

    logger.info(f"Processing repository '{repository_id}' from archive: {zip_path_str}")

    # Ensure repository record exists and is marked 'processing'
    repo = get_repository(db, repository_id)
    if not repo:
        repo = create_repository(db, repository_id=repository_id, zip_path=zip_path_str)
    
    update_repository(db, repository_id=repository_id, status="processing")

    zip_path = Path(zip_path_str)
    target_extract_dir = settings.extracted_dir / repository_id

    try:
        # Safe extraction (Zip Slip and file bomb protection)
        extracted_dir = SafeExtractor.extract_zip(zip_path, target_extract_dir)

        # Recursive code scanning
        code_files = RepositoryScanner.scan_directory(extracted_dir)
        total_files = len(code_files)
        logger.info(f"Repository '{repository_id}' scan complete: found {total_files} reviewable source files.")

        if total_files == 0:
            update_repository(
                db,
                repository_id=repository_id,
                status="completed",
                total_files=0,
                completed_files=0,
                failed_files=0,
            )
            return 0

        # Update total file count in repository table
        update_repository(db, repository_id=repository_id, total_files=total_files)

        # Dispatch individual review jobs
        for item in code_files:
            job_id = f"job-{uuid.uuid4().hex[:12]}"
            rel_path = item["file_path"]
            language = item["language"]

            # Save in database
            create_review_job(
                db=db,
                job_id=job_id,
                repository_id=repository_id,
                file_path=rel_path,
                language=language,
            )

            # Publish to review_queue
            queue_service.publish_review_job(
                job_id=job_id,
                repository_id=repository_id,
                file_path=rel_path,
                language=language,
                attempt=1,
            )

        logger.info(f"Successfully queued {total_files} review jobs for repository '{repository_id}'.")
        return total_files

    except Exception as e:
        logger.error(f"Failed to process repository '{repository_id}': {e}", exc_info=True)
        update_repository(db, repository_id=repository_id, status="failed")
        raise


def run_worker():
    """Main worker loop that connects to RabbitMQ and consumes repository jobs."""
    logger.info("Starting Repository Worker...")
    init_db()
    queue_service = RabbitMQService()

    def message_handler(payload: Dict[str, Any], channel, delivery_tag):
        db = SessionLocal()
        try:
            process_repository_job(payload, db, queue_service)
            channel.basic_ack(delivery_tag=delivery_tag)
            logger.info(f"Acknowledged repository job delivery_tag={delivery_tag}")
        except Exception as e:
            logger.error(f"Error in repository job handler: {e}")
            channel.basic_nack(delivery_tag=delivery_tag, requeue=False)
        finally:
            db.close()

    queue_service.consume(
        queue_name=settings.RABBITMQ_REPOSITORY_QUEUE,
        callback=message_handler,
        prefetch_count=1,
    )


if __name__ == "__main__":
    run_worker()
