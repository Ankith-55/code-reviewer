import os
import uuid
from pathlib import Path
from typing import Any
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from core.config import settings
from database.database import (
    get_db,
    create_repository,
    get_repository,
    get_repository_findings,
)
from services.rabbitmq import RabbitMQService
from schemas.review import (
    ReviewSubmissionResponse,
    ReviewStatusResponse,
    ReviewResultResponse,
    JobStatus,
    SeveritySummary,
    FindingItem,
)

router = APIRouter(tags=["review"])


def get_queue() -> Any:
    """Dependency provider for the message queue service."""
    return RabbitMQService()


@router.post(
    "/review",
    response_model=ReviewSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a repository ZIP for asynchronous code review",
)
async def submit_review(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    queue: Any = Depends(get_queue),
):
    # 1. Validate file extension
    filename = file.filename or ""
    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .zip files are accepted",
        )

    # 2. Check file size limit with streaming chunk write
    max_bytes = settings.MAX_ZIP_SIZE_MB * 1024 * 1024
    job_id = f"repo-{uuid.uuid4().hex[:8]}"
    upload_dir = settings.uploads_dir
    target_zip_path = upload_dir / f"{job_id}.zip"

    bytes_read = 0
    try:
        with open(target_zip_path, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):  # 1MB buffer
                bytes_read += len(chunk)
                if bytes_read > max_bytes:
                    target_zip_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"ZIP size exceeds maximum limit of {settings.MAX_ZIP_SIZE_MB}MB",
                    )
                buffer.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        target_zip_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save upload: {str(e)}",
        )

    # 3. Persist repository record in database
    create_repository(
        db=db,
        repository_id=job_id,
        zip_path=str(target_zip_path),
        total_files=0,
    )

    # 4. Dispatch job to repository queue
    try:
        queue.publish_repository_job(repository_id=job_id, zip_path=str(target_zip_path))
    except Exception as e:
        # If queue fails, mark as failed in DB
        from database.database import update_repository
        update_repository(db, repository_id=job_id, status="failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to enqueue repository review: {str(e)}",
        )

    return ReviewSubmissionResponse(job_id=job_id, status=JobStatus.QUEUED)


@router.get(
    "/review/{job_id}/status",
    response_model=ReviewStatusResponse,
    summary="Get the processing status of a repository review",
)
async def get_review_status(
    job_id: str,
    db: Session = Depends(get_db),
):
    repo = get_repository(db, job_id)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review job '{job_id}' not found",
        )

    # Get real-time job counts directly from ReviewJob records
    from database.models import ReviewJob
    completed_files = db.query(ReviewJob).filter(ReviewJob.repository_id == job_id, ReviewJob.status == "completed").count()
    failed_files = db.query(ReviewJob).filter(ReviewJob.repository_id == job_id, ReviewJob.status == "failed").count()
    
    current_status = repo.status
    if repo.total_files > 0 and (completed_files + failed_files) >= repo.total_files:
        current_status = "completed"

    return ReviewStatusResponse(
        job_id=repo.id,
        status=current_status,
        total_files=repo.total_files,
        completed_files=completed_files,
        failed_files=failed_files,
    )


@router.get(
    "/review/{job_id}",
    summary="Get the final code review report",
)
async def get_review_result(
    job_id: str,
    db: Session = Depends(get_db),
):
    repo = get_repository(db, job_id)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review job '{job_id}' not found",
        )

    from database.models import ReviewJob
    completed_files = db.query(ReviewJob).filter(ReviewJob.repository_id == job_id, ReviewJob.status == "completed").count()
    failed_files = db.query(ReviewJob).filter(ReviewJob.repository_id == job_id, ReviewJob.status == "failed").count()

    is_complete = repo.status == "completed" or (repo.total_files > 0 and (completed_files + failed_files) >= repo.total_files)
    if not is_complete:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "job_id": repo.id,
                "status": repo.status,
                "message": f"Review is currently {repo.status}. Please check back later.",
                "total_files": repo.total_files,
                "completed_files": completed_files,
                "failed_files": failed_files,
            },
        )

    # Aggregate findings
    db_findings = get_repository_findings(db, job_id)
    critical_count = 0
    high_count = 0
    medium_count = 0
    low_count = 0

    issues_list = []
    for f in db_findings:
        sev = f.severity.upper()
        if sev == "CRITICAL":
            critical_count += 1
        elif sev == "HIGH":
            high_count += 1
        elif sev == "MEDIUM":
            medium_count += 1
        elif sev == "LOW":
            low_count += 1

        issues_list.append(
            FindingItem(
                file_path=f.file_path,
                line=f.line,
                severity=sev,
                category=f.category,
                description=f.description,
                recommendation=f.recommendation,
            )
        )

    summary = SeveritySummary(
        critical=critical_count,
        high=high_count,
        medium=medium_count,
        low=low_count,
    )

    return ReviewResultResponse(
        job_id=repo.id,
        status=JobStatus.COMPLETED,
        files_analyzed=repo.completed_files,
        issues_found=len(issues_list),
        summary=summary,
        issues=issues_list,
    )
