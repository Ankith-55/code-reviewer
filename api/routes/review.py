import os
import uuid
import shutil
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, HTTPException, status
from fastapi.responses import JSONResponse

from core.config import settings
from schemas.review import (
    ReviewSubmissionResponse,
    ReviewStatusResponse,
    ReviewResultResponse,
    JobStatus,
    SeveritySummary,
    FindingItem,
)

router = APIRouter(tags=["review"])

# In-memory store for skeleton/fallback testing before DB is attached
_MOCK_JOBS: dict[str, dict] = {}


@router.post(
    "/review",
    response_model=ReviewSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a repository ZIP for code review",
)
async def submit_review(file: UploadFile = File(...)):
    # 1. Validate file extension
    filename = file.filename or ""
    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .zip files are accepted",
        )

    # 2. Check file size limit (streaming chunk check)
    max_bytes = settings.MAX_ZIP_SIZE_MB * 1024 * 1024
    job_id = f"repo-{uuid.uuid4().hex[:8]}"
    upload_dir = settings.uploads_dir
    target_zip_path = upload_dir / f"{job_id}.zip"

    bytes_read = 0
    try:
        with open(target_zip_path, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):  # 1MB chunks
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

    # Record initial state
    _MOCK_JOBS[job_id] = {
        "status": JobStatus.QUEUED,
        "total_files": 0,
        "completed_files": 0,
        "failed_files": 0,
        "zip_path": str(target_zip_path),
        "issues": [],
    }

    return ReviewSubmissionResponse(job_id=job_id, status=JobStatus.QUEUED)


@router.get(
    "/review/{job_id}/status",
    response_model=ReviewStatusResponse,
    summary="Get the progress status of a repository review",
)
async def get_review_status(job_id: str):
    if job_id not in _MOCK_JOBS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review job '{job_id}' not found",
        )

    job_data = _MOCK_JOBS[job_id]
    return ReviewStatusResponse(
        job_id=job_id,
        status=job_data["status"],
        total_files=job_data["total_files"],
        completed_files=job_data["completed_files"],
        failed_files=job_data["failed_files"],
    )


@router.get(
    "/review/{job_id}",
    summary="Get the final code review report",
)
async def get_review_result(job_id: str):
    if job_id not in _MOCK_JOBS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review job '{job_id}' not found",
        )

    job_data = _MOCK_JOBS[job_id]
    if job_data["status"] != JobStatus.COMPLETED:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "job_id": job_id,
                "status": job_data["status"],
                "message": f"Review is currently {job_data['status']}. Please check back later.",
                "total_files": job_data["total_files"],
                "completed_files": job_data["completed_files"],
            },
        )

    issues = job_data.get("issues", [])
    summary = SeveritySummary(
        critical=sum(1 for i in issues if i.get("severity") == "CRITICAL"),
        high=sum(1 for i in issues if i.get("severity") == "HIGH"),
        medium=sum(1 for i in issues if i.get("severity") == "MEDIUM"),
        low=sum(1 for i in issues if i.get("severity") == "LOW"),
    )

    return ReviewResultResponse(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        files_analyzed=job_data["completed_files"],
        issues_found=len(issues),
        summary=summary,
        issues=issues,
    )
