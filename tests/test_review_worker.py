import os
from pathlib import Path
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import settings
from database.models import Base
from database.database import (
    init_db,
    create_repository,
    get_repository,
    create_review_job,
    get_review_job,
    get_repository_findings,
)
from services.llm_service import MockLLMService, BaseLLMService
from workers.review_worker import process_review_job


@pytest.fixture
def db_session():
    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def test_mock_llm_pattern_detection():
    llm = MockLLMService(simulated_delay_ms=0)
    vulnerable_code = """
import os

password = "supersecretpassword123"
query = "SELECT * FROM users WHERE id = " + user_input
eval(user_input)

try:
    do_something()
except:
    pass

# TODO: fix this later
"""
    findings = llm.analyze_code(vulnerable_code, language="python", file_path="vuln.py")
    categories = [f["category"] for f in findings]
    severities = [f["severity"] for f in findings]

    assert "security" in categories
    assert "CRITICAL" in severities  # eval
    assert "HIGH" in severities      # password, sql injection
    assert "bug" in categories       # broad except
    assert "performance" in categories  # SELECT *
    assert "maintainability" in categories  # TODO comment


def test_llm_json_cleaning_markdown_fences():
    raw_response = """
Here is the review result:
```json
{
    "issues": [
        {
            "line": 10,
            "severity": "CRITICAL",
            "category": "security",
            "description": "Remote code execution",
            "recommendation": "Sanitize inputs"
        }
    ]
}
```
Hope this helps!
"""
    parsed = BaseLLMService.clean_and_parse_json(raw_response)
    assert len(parsed) == 1
    assert parsed[0]["severity"] == "CRITICAL"
    assert parsed[0]["line"] == 10


def test_llm_json_malformed_graceful_handling():
    malformed_response = "Sorry, I encountered an internal error and cannot generate JSON."
    parsed = BaseLLMService.clean_and_parse_json(malformed_response)
    assert parsed == []


def test_process_review_job_success(db_session, tmp_path):
    repo_id = "test-repo-review-1"
    job_id = "test-job-review-1"
    rel_file = "src/payment.py"

    # Setup extracted file on disk
    extracted_dir = settings.extracted_dir / repo_id / "src"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    target_file = extracted_dir / "payment.py"
    target_file.write_text("password = 'unencrypted_secret'\n", encoding="utf-8")

    # DB records
    create_repository(db_session, repository_id=repo_id, total_files=1)
    create_review_job(db_session, job_id=job_id, repository_id=repo_id, file_path=rel_file, language="python")

    llm = MockLLMService(simulated_delay_ms=0)
    payload = {
        "job_id": job_id,
        "repository_id": repo_id,
        "file_path": rel_file,
        "language": "python",
        "attempt": 1,
    }

    finding_count = process_review_job(payload, db_session, llm)
    assert finding_count >= 1

    # Verify review job completed
    job = get_review_job(db_session, job_id)
    assert job.status == "completed"
    assert job.completed_at is not None

    # Verify repository auto-completed
    repo = get_repository(db_session, repo_id)
    assert repo.completed_files == 1
    assert repo.status == "completed"

    # Verify findings saved in DB
    findings = get_repository_findings(db_session, repo_id)
    assert len(findings) == finding_count
    assert any(f.severity == "HIGH" for f in findings)


def test_process_review_job_missing_file(db_session):
    repo_id = "test-repo-missing"
    job_id = "test-job-missing"

    create_repository(db_session, repository_id=repo_id, total_files=1)
    create_review_job(db_session, job_id=job_id, repository_id=repo_id, file_path="ghost.py")

    llm = MockLLMService(simulated_delay_ms=0)
    payload = {
        "job_id": job_id,
        "repository_id": repo_id,
        "file_path": "ghost.py",
        "language": "python",
    }

    with pytest.raises(FileNotFoundError):
        process_review_job(payload, db_session, llm)

    job = get_review_job(db_session, job_id)
    assert job.status == "failed"
    assert "File not found" in job.error_message

    repo = get_repository(db_session, repo_id)
    assert repo.failed_files == 1
    assert repo.status == "completed"  # All files accounted for (1 failed out of 1)
