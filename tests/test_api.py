import io
import zipfile
import pytest
from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)


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
    # Build a small in-memory zip
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("main.py", "def add(a, b):\n    return a + b\n")

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

    # Check status endpoint
    status_response = client.get(f"/review/{job_id}/status")
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["job_id"] == job_id
    assert status_data["status"] == "queued"


def test_get_nonexistent_job_status():
    response = client.get("/review/repo-does-not-exist/status")
    assert response.status_code == 404
