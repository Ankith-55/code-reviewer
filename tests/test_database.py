import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, Repository, ReviewJob, Finding
from database.database import (
    init_db,
    create_repository,
    get_repository,
    update_repository,
    increment_repo_file_counters,
    create_review_job,
    get_review_job,
    update_review_job_status,
    save_findings,
    get_repository_findings,
)


@pytest.fixture
def db_session():
    """In-memory SQLite database session fixture for testing."""
    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_repository_crud(db_session):
    repo = create_repository(db_session, repository_id="repo-test-1", zip_path="/tmp/test.zip", total_files=5)
    assert repo.id == "repo-test-1"
    assert repo.status == "queued"
    assert repo.total_files == 5
    assert repo.completed_files == 0

    fetched = get_repository(db_session, "repo-test-1")
    assert fetched is not None
    assert fetched.id == "repo-test-1"

    updated = update_repository(db_session, "repo-test-1", status="processing")
    assert updated.status == "processing"


def test_review_jobs_and_findings(db_session):
    repo = create_repository(db_session, repository_id="repo-test-2", total_files=2)
    job1 = create_review_job(db_session, job_id="job-1", repository_id=repo.id, file_path="auth.py", language="python")
    job2 = create_review_job(db_session, job_id="job-2", repository_id=repo.id, file_path="db.py", language="python")

    assert job1.status == "queued"
    assert job1.attempts == 0

    # Update job 1
    update_review_job_status(db_session, "job-1", status="completed", increment_attempts=True)
    job1_updated = get_review_job(db_session, "job-1")
    assert job1_updated.status == "completed"
    assert job1_updated.attempts == 1

    # Add findings to job 1
    sample_findings = [
        {
            "line": 42,
            "severity": "HIGH",
            "category": "security",
            "description": "SQL Injection risk",
            "recommendation": "Use parameterized queries",
        },
        {
            "line": 15,
            "severity": "LOW",
            "category": "style",
            "description": "Unused import",
            "recommendation": "Remove unused import",
        },
    ]
    saved = save_findings(db_session, review_job_id="job-1", file_path="auth.py", findings=sample_findings)
    assert len(saved) == 2

    # Query findings by repository
    repo_findings = get_repository_findings(db_session, "repo-test-2")
    assert len(repo_findings) == 2
    assert repo_findings[0].severity == "HIGH"
    assert repo_findings[0].category == "security"


def test_increment_and_auto_completion(db_session):
    repo = create_repository(db_session, repository_id="repo-test-3", total_files=2)
    assert repo.status == "queued"

    # Complete 1 file
    increment_repo_file_counters(db_session, "repo-test-3", completed_inc=1)
    fetched = get_repository(db_session, "repo-test-3")
    assert fetched.completed_files == 1
    assert fetched.status == "queued"  # still waiting for file 2

    # Complete 2nd file (or 1 fail)
    increment_repo_file_counters(db_session, "repo-test-3", failed_inc=1)
    fetched = get_repository(db_session, "repo-test-3")
    assert fetched.completed_files == 1
    assert fetched.failed_files == 1
    assert fetched.status == "completed"
    assert fetched.completed_at is not None
