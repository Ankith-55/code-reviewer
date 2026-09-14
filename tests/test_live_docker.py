import io
import time
import zipfile
import httpx


def test_live_docker_flow():
    base_url = "http://localhost:8000"

    # 1. Health check
    health = httpx.get(f"{base_url}/health")
    assert health.status_code == 200
    print("[1/4] Docker API is healthy.")

    # 2. Upload sample repository with intentional issues
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("app/auth.py", "password = 'unencrypted_secret_pass'\n")
        zf.writestr("app/runner.py", "eval('2 * 3')\n")
        zf.writestr("app/data.py", "query = 'SELECT * FROM accounts WHERE id=' + uid\n")
        zf.writestr("app/clean.py", "def add(a, b):\n    return a + b\n")

    zip_buf.seek(0)
    upload_res = httpx.post(
        f"{base_url}/review",
        files={"file": ("live_repo.zip", zip_buf.getvalue(), "application/zip")},
    )
    assert upload_res.status_code == 202
    job_id = upload_res.json()["job_id"]
    print(f"[2/4] Uploaded live_repo.zip -> Job ID: {job_id}")

    # 3. Poll status until completed (timeout: 30s)
    completed = False
    for attempt in range(30):
        status_res = httpx.get(f"{base_url}/review/{job_id}/status")
        assert status_res.status_code == 200
        data = status_res.json()
        print(f"      Polling status ({attempt + 1}s): {data['status']} "
              f"(Total: {data['total_files']}, Completed: {data['completed_files']})")

        if data["status"] == "completed":
            completed = True
            break
        time.sleep(1)

    assert completed, "Review did not complete in time"
    print("[3/4] Repository review completed successfully across distributed workers!")

    # 4. Fetch final report
    report_res = httpx.get(f"{base_url}/review/{job_id}")
    assert report_res.status_code == 200
    report = report_res.json()
    print(f"[4/4] Report received: {report['issues_found']} issues detected.")
    print("      Summary:", report["summary"])
    for issue in report["issues"]:
        print(f"      - [{issue['severity']}] {issue['file_path']}: {issue['description']}")


if __name__ == "__main__":
    test_live_docker_flow()
