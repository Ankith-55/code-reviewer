import io
import zipfile
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.main import app
from api.routes.review import get_db, get_queue
from database.models import Base
from database.database import init_db, get_repository, update_repository, save_findings, create_review_job
from services.rabbitmq import InMemoryQueue
from services.llm_service import MockLLMService
from workers.repository_worker import process_repository_job
from workers.review_worker import process_review_job
from core.config import settings

from sqlalchemy.pool import StaticPool

# Test engine sharing same in-memory DB across threads
test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
init_db(test_engine)

test_queue = InMemoryQueue()


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def override_get_queue():
    return test_queue


app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_queue] = override_get_queue

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_and_teardown():
    # Clear test queues before each test
    test_queue.queues[settings.RABBITMQ_REPOSITORY_QUEUE].clear()
    test_queue.queues[settings.RABBITMQ_REVIEW_QUEUE].clear()
    test_queue.queues[settings.RABBITMQ_DEAD_LETTER_QUEUE].clear()
    yield


def test_root_endpoint():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "online"
    assert "docs_url" in data


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


def test_reject_non_zip_file():
    file_content = b"print('hello world')"
    response = client.post(
        "/review",
        files={"file": ("main.py", file_content, "text/x-python")},
    )
    assert response.status_code == 400
    assert "Only .zip files are accepted" in response.json()["detail"]


def test_submit_valid_zip():
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("app/main.py", "eval('1 + 1')\n")

    zip_buffer.seek(0)
    response = client.post(
        "/review",
        files={"file": ("test_repo.zip", zip_buffer.getvalue(), "application/zip")},
    )
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == "queued"

    job_id = data["job_id"]

    # Verify status endpoint returns queued
    status_response = client.get(f"/review/{job_id}/status")
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["job_id"] == job_id
    assert status_data["status"] == "queued"

    # Verify message was queued in repository_queue
    queued_repo_jobs = test_queue.queues[settings.RABBITMQ_REPOSITORY_QUEUE]
    assert len(queued_repo_jobs) == 1
    assert queued_repo_jobs[0]["repository_id"] == job_id


def test_get_nonexistent_job_status():
    response = client.get("/review/repo-does-not-exist/status")
    assert response.status_code == 404


def test_full_pipeline_e2e():
    """Simulates the entire distributed lifecycle:

    1. User uploads ZIP via API
    2. Repository worker pulls from repository_queue and creates review jobs
    3. Review worker pulls from review_queue, calls LLM, saves findings
    4. User queries GET /review/{job_id} and gets structured review report
    """
    # 1. Upload ZIP
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("server/auth.py", "password = 'hardcoded_secret'\n")
        zf.writestr("server/db.py", "query = 'SELECT * FROM users WHERE id = ' + uid\n")

    zip_buffer.seek(0)
    upload_res = client.post(
        "/review",
        files={"file": ("full_project.zip", zip_buffer.getvalue(), "application/zip")},
    )
    assert upload_res.status_code == 202
    job_id = upload_res.json()["job_id"]

    # 2. In-progress report check
    report_in_progress = client.get(f"/review/{job_id}")
    assert report_in_progress.status_code == 200
    assert report_in_progress.json()["status"] == "queued"

    # 3. Simulate Repository Worker
    db = TestingSessionLocal()
    repo_msg = test_queue.queues[settings.RABBITMQ_REPOSITORY_QUEUE].pop(0)
    dispatched_count = process_repository_job(repo_msg, db, test_queue)
    assert dispatched_count == 2
    assert len(test_queue.queues[settings.RABBITMQ_REVIEW_QUEUE]) == 2

    # 4. Simulate Review Workers processing both files
    llm = MockLLMService(simulated_delay_ms=0)
    while test_queue.queues[settings.RABBITMQ_REVIEW_QUEUE]:
        review_msg = test_queue.queues[settings.RABBITMQ_REVIEW_QUEUE].pop(0)
        process_review_job(review_msg, db, llm)

    # 5. Check completed status
    status_res = client.get(f"/review/{job_id}/status")
    assert status_res.status_code == 200
    status_info = status_res.json()
    assert status_info["status"] == "completed"
    assert status_info["total_files"] == 2
    assert status_info["completed_files"] == 2
    assert status_info["failed_files"] == 0

    # 6. Fetch Final Report
    final_res = client.get(f"/review/{job_id}")
    assert final_res.status_code == 200
    report = final_res.json()
    assert report["status"] == "completed"
    assert report["files_analyzed"] == 2
    assert report["issues_found"] >= 2
    assert report["summary"]["high"] >= 2  # Hardcoded password + SQL injection
    assert len(report["issues"]) == report["issues_found"]

    db.close()
