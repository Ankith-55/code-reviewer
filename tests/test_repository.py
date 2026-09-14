import io
import os
import zipfile
from pathlib import Path
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import settings
from database.models import Base
from database.database import init_db, get_repository, get_review_job
from services.repository_service import (
    SafeExtractor,
    RepositoryScanner,
    RepositoryServiceError,
    LANGUAGE_EXTENSIONS,
)
from services.rabbitmq import InMemoryQueue
from workers.repository_worker import process_repository_job


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


@pytest.fixture
def temp_repo_zip(tmp_path):
    """Creates a sample test repository ZIP with nested structure and ignored files."""
    zip_path = tmp_path / "sample_project.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Valid source files in nested directories
        zf.writestr("project/main.py", "def main():\n    print('hello')\n")
        zf.writestr("project/services/auth.ts", "export function login() { return true; }\n")
        zf.writestr("project/models/user.go", "package models\ntype User struct{}\n")
        zf.writestr("project/cpp/math.cpp", "int add(int a, int b) { return a + b; }\n")

        # Files that should be ignored
        zf.writestr("project/.git/config", "ignored git file")
        zf.writestr("project/node_modules/package.js", "console.log('ignored node');")
        zf.writestr("project/__pycache__/cache.pyc", "ignored pycache")
        zf.writestr("project/assets/logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        zf.writestr("project/binary.py", b"\x00\x00\x01\x02binary code with null bytes")

    return zip_path


def test_safe_extractor_valid(tmp_path, temp_repo_zip):
    target_dir = tmp_path / "extracted"
    extracted = SafeExtractor.extract_zip(temp_repo_zip, target_dir)
    assert extracted.exists()
    assert (extracted / "project" / "main.py").exists()


def test_safe_extractor_path_traversal_detection(tmp_path):
    malicious_zip = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious_zip, "w") as zf:
        zf.writestr("../evil.py", "malicious payload")

    target_dir = tmp_path / "extracted_safe"
    with pytest.raises(RepositoryServiceError) as exc_info:
        SafeExtractor.extract_zip(malicious_zip, target_dir)

    assert "traversal" in str(exc_info.value).lower() or "unsafe path" in str(exc_info.value).lower()


def test_safe_extractor_max_files_limit(tmp_path):
    bomb_zip = tmp_path / "too_many_files.zip"
    with zipfile.ZipFile(bomb_zip, "w") as zf:
        for i in range(15):
            zf.writestr(f"file_{i}.txt", "data")

    target_dir = tmp_path / "extracted_bomb"
    with pytest.raises(RepositoryServiceError) as exc_info:
        SafeExtractor.extract_zip(bomb_zip, target_dir, max_files=10)

    assert "exceeding maximum allowed limit" in str(exc_info.value)


def test_repository_scanner(tmp_path, temp_repo_zip):
    target_dir = tmp_path / "extracted"
    SafeExtractor.extract_zip(temp_repo_zip, target_dir)

    files = RepositoryScanner.scan_directory(target_dir)
    file_paths = [f["file_path"] for f in files]
    languages = {f["file_path"]: f["language"] for f in files}

    # Should find valid files
    assert any("main.py" in p for p in file_paths)
    assert any("auth.ts" in p for p in file_paths)
    assert any("user.go" in p for p in file_paths)
    assert any("math.cpp" in p for p in file_paths)

    # Check language detection
    main_py = next(p for p in file_paths if "main.py" in p)
    auth_ts = next(p for p in file_paths if "auth.ts" in p)
    assert languages[main_py] == "python"
    assert languages[auth_ts] == "typescript"

    # Should ignore unwanted folders and files
    assert not any(".git" in p for p in file_paths)
    assert not any("node_modules" in p for p in file_paths)
    assert not any("__pycache__" in p for p in file_paths)
    assert not any("logo.png" in p for p in file_paths)
    assert not any("binary.py" in p for p in file_paths)


def test_process_repository_job_end_to_end(db_session, temp_repo_zip):
    mock_queue = InMemoryQueue()
    repo_id = "test-repo-e2e"

    payload = {
        "repository_id": repo_id,
        "zip_path": str(temp_repo_zip),
    }

    count = process_repository_job(payload, db_session, mock_queue)
    assert count == 4  # main.py, auth.ts, user.go, math.cpp

    # Verify repository status in database
    repo = get_repository(db_session, repo_id)
    assert repo is not None
    assert repo.total_files == 4
    assert repo.status == "processing"

    # Verify jobs were placed in review queue
    queued_jobs = mock_queue.queues[settings.RABBITMQ_REVIEW_QUEUE]
    assert len(queued_jobs) == 4

    for qj in queued_jobs:
        assert qj["repository_id"] == repo_id
        assert qj["language"] in ["python", "typescript", "go", "cpp"]
        # Verify job was recorded in database
        db_job = get_review_job(db_session, qj["job_id"])
        assert db_job is not None
        assert db_job.status == "queued"
