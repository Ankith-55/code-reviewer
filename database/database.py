import datetime
from typing import Generator, List, Optional, Dict, Any
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker, Session
from core.config import settings
from database.models import Base, Repository, ReviewJob, Finding

# Engine creation: supports postgresql and sqlite (used in testing/isolated environments)
_db_url = settings.sync_database_url
_connect_args = {}
if _db_url.startswith("sqlite"):
    _connect_args["check_same_thread"] = False

engine = create_engine(
    _db_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db(target_engine=None):
    """Initializes the database schema if not already present."""
    eg = target_engine or engine
    Base.metadata.create_all(bind=eg)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for yielding DB sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -------------------------------------------------------------
# Clean Database Abstraction Functions (DAO layer)
# -------------------------------------------------------------

def create_repository(
    db: Session,
    repository_id: str,
    zip_path: Optional[str] = None,
    total_files: int = 0,
) -> Repository:
    repo = Repository(
        id=repository_id,
        zip_path=zip_path,
        status="queued",
        total_files=total_files,
        completed_files=0,
        failed_files=0,
        created_at=datetime.datetime.utcnow(),
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo


def get_repository(db: Session, repository_id: str) -> Optional[Repository]:
    return db.query(Repository).filter(Repository.id == repository_id).first()


def update_repository(
    db: Session,
    repository_id: str,
    status: Optional[str] = None,
    total_files: Optional[int] = None,
    completed_files: Optional[int] = None,
    failed_files: Optional[int] = None,
    completed_at: Optional[datetime.datetime] = None,
) -> Optional[Repository]:
    repo = get_repository(db, repository_id)
    if not repo:
        return None

    if status is not None:
        repo.status = status
    if total_files is not None:
        repo.total_files = total_files
    if completed_files is not None:
        repo.completed_files = completed_files
    if failed_files is not None:
        repo.failed_files = failed_files
    if completed_at is not None:
        repo.completed_at = completed_at

    db.commit()
    db.refresh(repo)
    return repo


def increment_repo_file_counters(
    db: Session,
    repository_id: str,
    completed_inc: int = 0,
    failed_inc: int = 0,
) -> Optional[Repository]:
    repo = get_repository(db, repository_id)
    if not repo:
        return None

    repo.completed_files += completed_inc
    repo.failed_files += failed_inc

    # Automatically mark repository completed if all files processed
    if repo.total_files > 0 and (repo.completed_files + repo.failed_files) >= repo.total_files:
        repo.status = "completed"
        repo.completed_at = datetime.datetime.utcnow()

    db.commit()
    db.refresh(repo)
    return repo


def create_review_job(
    db: Session,
    job_id: str,
    repository_id: str,
    file_path: str,
    language: str = "unknown",
) -> ReviewJob:
    job = ReviewJob(
        id=job_id,
        repository_id=repository_id,
        file_path=file_path,
        language=language,
        status="queued",
        attempts=0,
        created_at=datetime.datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_review_job(db: Session, job_id: str) -> Optional[ReviewJob]:
    return db.query(ReviewJob).filter(ReviewJob.id == job_id).first()


def update_review_job_status(
    db: Session,
    job_id: str,
    status: str,
    started_at: Optional[datetime.datetime] = None,
    completed_at: Optional[datetime.datetime] = None,
    error_message: Optional[str] = None,
    increment_attempts: bool = False,
) -> Optional[ReviewJob]:
    job = get_review_job(db, job_id)
    if not job:
        return None

    job.status = status
    if started_at:
        job.started_at = started_at
    if completed_at:
        job.completed_at = completed_at
    if error_message:
        job.error_message = error_message
    if increment_attempts:
        job.attempts += 1

    db.commit()
    db.refresh(job)
    return job


def save_findings(
    db: Session,
    review_job_id: str,
    file_path: str,
    findings: List[Dict[str, Any]],
) -> List[Finding]:
    created_findings = []
    for f in findings:
        finding = Finding(
            review_job_id=review_job_id,
            file_path=file_path,
            line=f.get("line"),
            severity=f.get("severity", "MEDIUM").upper(),
            category=f.get("category", "bug").lower(),
            description=f.get("description", ""),
            recommendation=f.get("recommendation", ""),
            created_at=datetime.datetime.utcnow(),
        )
        db.add(finding)
        created_findings.append(finding)

    db.commit()
    return created_findings


def get_repository_findings(db: Session, repository_id: str) -> List[Finding]:
    return (
        db.query(Finding)
        .join(ReviewJob, Finding.review_job_id == ReviewJob.id)
        .filter(ReviewJob.repository_id == repository_id)
        .all()
    )
