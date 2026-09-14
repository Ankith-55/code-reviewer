import pytest
from services.rabbitmq import InMemoryQueue, RabbitMQService
from core.config import settings


def test_in_memory_queue_publish_and_ack():
    queue = InMemoryQueue()
    queue.publish_repository_job("repo-123", "/path/to/repo.zip")

    msg_tuple = queue.get_message(settings.RABBITMQ_REPOSITORY_QUEUE)
    assert msg_tuple is not None
    payload, tag = msg_tuple

    assert payload["repository_id"] == "repo-123"
    assert payload["zip_path"] == "/path/to/repo.zip"
    assert tag in queue.unacked

    # Acknowledge
    queue.ack(tag)
    assert tag not in queue.unacked
    assert queue.get_message(settings.RABBITMQ_REPOSITORY_QUEUE) is None


def test_worker_crash_and_redelivery():
    queue = InMemoryQueue()
    queue.publish_review_job("job-abc", "repo-123", "src/auth.py", "python")

    # Worker 1 picks up message
    payload1, tag1 = queue.get_message(settings.RABBITMQ_REVIEW_QUEUE)
    assert payload1["job_id"] == "job-abc"
    assert tag1 in queue.unacked

    # Worker 1 crashes before acknowledging
    queue.simulate_worker_crash(tag1)

    # Message must now be back in queue for Worker 2 to consume
    payload2, tag2 = queue.get_message(settings.RABBITMQ_REVIEW_QUEUE)
    assert payload2 is not None
    assert payload2["job_id"] == "job-abc"
    assert tag2 != tag1  # New delivery tag

    # Worker 2 processes and acknowledges
    queue.ack(tag2)
    assert len(queue.unacked) == 0
    assert queue.get_message(settings.RABBITMQ_REVIEW_QUEUE) is None


def test_dead_letter_queue_routing():
    queue = InMemoryQueue()
    queue.publish_review_job("poison-job", "repo-123", "corrupt.bin", "unknown")

    payload, tag = queue.get_message(settings.RABBITMQ_REVIEW_QUEUE)
    assert payload["job_id"] == "poison-job"

    # Reject without requeue (send to DLQ)
    queue.nack(tag, requeue=False)

    assert queue.get_message(settings.RABBITMQ_REVIEW_QUEUE) is None
    dlq_payload, dlq_tag = queue.get_message(settings.RABBITMQ_DEAD_LETTER_QUEUE)
    assert dlq_payload is not None
    assert dlq_payload["job_id"] == "poison-job"
    queue.ack(dlq_tag)


def test_rabbitmq_service_parameters():
    service = RabbitMQService(host="test-host", port=5672, user="test_user", password="secret_password")
    assert service.host == "test-host"
    assert service.port == 5672
    assert service.parameters.host == "test-host"
    assert service.parameters.port == 5672
