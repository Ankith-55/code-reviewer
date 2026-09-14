from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class IssueCategory(str, Enum):
    BUG = "bug"
    SECURITY = "security"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"
    STYLE = "style"


class ReviewSubmissionResponse(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.QUEUED


class ReviewStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    total_files: int = 0
    completed_files: int = 0
    failed_files: int = 0


class FindingItem(BaseModel):
    file_path: str
    line: Optional[int] = None
    severity: str
    category: str
    description: str
    recommendation: str


class SeveritySummary(BaseModel):
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0


class ReviewResultResponse(BaseModel):
    job_id: str
    status: JobStatus
    files_analyzed: int
    issues_found: int
    summary: SeveritySummary
    issues: List[FindingItem] = Field(default_factory=list)


# RabbitMQ Message Schemas
class RepositoryJobMessage(BaseModel):
    repository_id: str
    zip_path: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ReviewJobMessage(BaseModel):
    job_id: str
    repository_id: str
    file_path: str
    language: str
    attempt: int = 1
