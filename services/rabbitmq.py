import json
import logging
from typing import Callable, Optional, Dict, Any
import pika
from pika.exceptions import AMQPConnectionError, AMQPChannelError

from core.config import settings

logger = logging.getLogger(__name__)


class RabbitMQService:
    """Production RabbitMQ service supporting durable queues, persistent messages,

    fair dispatch (QoS prefetch), acknowledgements, and dead-letter queueing.
    """

    def __init__(
        self,
        host: str = settings.RABBITMQ_HOST,
        port: int = settings.RABBITMQ_PORT,
        user: str = settings.RABBITMQ_USER,
        password: str = settings.RABBITMQ_PASSWORD,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.credentials = pika.PlainCredentials(self.user, self.password)
        self.parameters = pika.ConnectionParameters(
            host=self.host,
            port=self.port,
            credentials=self.credentials,
            heartbeat=600,
            blocked_connection_timeout=300,
        )

    def get_connection(self) -> pika.BlockingConnection:
        """Establishes and returns a blocking connection to RabbitMQ."""
        return pika.BlockingConnection(self.parameters)

    def setup_queues(self, channel: pika.adapters.blocking_connection.BlockingChannel):
        """Declares all queues, dead-letter exchanges, and routing topologies.

        1. dlx_exchange -> routes failed messages to review_dlq
        2. repository_queue: durable
        3. review_queue: durable, DLX bound
        4. review_dlq: durable
        """
        # 1. Dead Letter Exchange & Queue
        channel.exchange_declare(
            exchange="dlx_exchange",
            exchange_type="direct",
            durable=True,
        )
        channel.queue_declare(
            queue=settings.RABBITMQ_DEAD_LETTER_QUEUE,
            durable=True,
        )
        channel.queue_bind(
            exchange="dlx_exchange",
            queue=settings.RABBITMQ_DEAD_LETTER_QUEUE,
            routing_key=settings.RABBITMQ_DEAD_LETTER_QUEUE,
        )

        # 2. Repository Queue
        channel.queue_declare(
            queue=settings.RABBITMQ_REPOSITORY_QUEUE,
            durable=True,
        )

        # 3. Review Queue with DLX configured
        channel.queue_declare(
            queue=settings.RABBITMQ_REVIEW_QUEUE,
            durable=True,
            arguments={
                "x-dead-letter-exchange": "dlx_exchange",
                "x-dead-letter-routing-key": settings.RABBITMQ_DEAD_LETTER_QUEUE,
            },
        )

    def publish_repository_job(self, repository_id: str, zip_path: str) -> bool:
        """Publishes a repository extraction/scan job to the repository queue."""
        payload = {
            "repository_id": repository_id,
            "zip_path": zip_path,
        }
        return self._publish(settings.RABBITMQ_REPOSITORY_QUEUE, payload)

    def publish_review_job(
        self,
        job_id: str,
        repository_id: str,
        file_path: str,
        language: str,
        attempt: int = 1,
    ) -> bool:
        """Publishes an individual source file review job to the review queue."""
        payload = {
            "job_id": job_id,
            "repository_id": repository_id,
            "file_path": file_path,
            "language": language,
            "attempt": attempt,
        }
        return self._publish(settings.RABBITMQ_REVIEW_QUEUE, payload)

    def _publish(self, queue_name: str, payload: Dict[str, Any]) -> bool:
        """Publishes a persistent JSON message to the specified queue."""
        connection = None
        try:
            connection = self.get_connection()
            channel = connection.channel()
            self.setup_queues(channel)

            message_body = json.dumps(payload)
            channel.basic_publish(
                exchange="",
                routing_key=queue_name,
                body=message_body.encode("utf-8"),
                properties=pika.BasicProperties(
                    delivery_mode=pika.DeliveryMode.Persistent,
                    content_type="application/json",
                ),
            )
            logger.info(f"Published message to queue '{queue_name}': {payload}")
            return True
        except (AMQPConnectionError, AMQPChannelError) as e:
            logger.error(f"Failed to publish message to queue '{queue_name}': {e}")
            raise
        finally:
            if connection and connection.is_open:
                connection.close()

    def consume(
        self,
        queue_name: str,
        callback: Callable[[Dict[str, Any], pika.adapters.blocking_connection.BlockingChannel, Any], None],
        prefetch_count: int = 1,
    ):
        """Starts consuming messages from a queue with fair prefetch and manual ack.

        The worker callback receives:
            (payload_dict, channel, method.delivery_tag)
        and must explicitly ack or nack.
        """
        connection = self.get_connection()
        channel = connection.channel()
        self.setup_queues(channel)

        # Fair dispatch: give only 1 unacked message to this worker at a time
        channel.basic_qos(prefetch_count=prefetch_count)

        def on_message_received(ch, method, properties, body):
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception as e:
                logger.error(f"Malformed JSON in queue '{queue_name}': {e}. Rejecting to DLQ.")
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            try:
                callback(payload, ch, method.delivery_tag)
            except Exception as e:
                logger.error(f"Error handling message {payload}: {e}")
                # Worker-level error: decide whether to retry or route to DLQ
                attempt = payload.get("attempt", 1)
                if attempt < 3:
                    logger.warning(f"Requeuing job (attempt {attempt + 1}/3)...")
                    payload["attempt"] = attempt + 1
                    # Acknowledge the failed delivery and republish with updated attempt count
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    self._publish(queue_name, payload)
                else:
                    logger.error(f"Max attempts reached for {payload}. Routing to DLQ.")
                    ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

        channel.basic_consume(
            queue=queue_name,
            on_message_callback=on_message_received,
            auto_ack=False,  # MANUAL ACK ONLY
        )

        logger.info(f"Worker listening on queue '{queue_name}' with prefetch={prefetch_count}...")
        try:
            channel.start_consuming()
        except KeyboardInterrupt:
            logger.info("Consumer stopped by KeyboardInterrupt.")
            channel.stop_consuming()
        finally:
            if connection.is_open:
                connection.close()


# In-memory queue implementation for fast, fully isolated local testing & mocking
class InMemoryQueue:
    """Mock queue implementation providing FIFO ordering, manual ack/nack,

    redelivery semantics, and DLQ for reliable local testing without RabbitMQ.
    """

    def __init__(self):
        self.queues: Dict[str, list] = {
            settings.RABBITMQ_REPOSITORY_QUEUE: [],
            settings.RABBITMQ_REVIEW_QUEUE: [],
            settings.RABBITMQ_DEAD_LETTER_QUEUE: [],
        }
        self.unacked: Dict[int, Dict[str, Any]] = {}
        self._tag_counter = 0

    def publish_repository_job(self, repository_id: str, zip_path: str) -> bool:
        self.queues[settings.RABBITMQ_REPOSITORY_QUEUE].append({
            "repository_id": repository_id,
            "zip_path": zip_path,
        })
        return True

    def publish_review_job(
        self,
        job_id: str,
        repository_id: str,
        file_path: str,
        language: str,
        attempt: int = 1,
    ) -> bool:
        self.queues[settings.RABBITMQ_REVIEW_QUEUE].append({
            "job_id": job_id,
            "repository_id": repository_id,
            "file_path": file_path,
            "language": language,
            "attempt": attempt,
        })
        return True

    def get_message(self, queue_name: str) -> Optional[tuple[Dict[str, Any], int]]:
        """Pulls next message and tracks it in unacked store until acked or nacked."""
        q = self.queues.get(queue_name, [])
        if not q:
            return None

        payload = q.pop(0)
        self._tag_counter += 1
        tag = self._tag_counter
        self.unacked[tag] = {"queue": queue_name, "payload": payload}
        return payload, tag

    def ack(self, delivery_tag: int):
        self.unacked.pop(delivery_tag, None)

    def nack(self, delivery_tag: int, requeue: bool = False):
        item = self.unacked.pop(delivery_tag, None)
        if not item:
            return
        if requeue:
            # Place back at head of queue (redelivery)
            self.queues[item["queue"]].insert(0, item["payload"])
        else:
            # Route to dead letter queue
            self.queues[settings.RABBITMQ_DEAD_LETTER_QUEUE].append(item["payload"])

    def simulate_worker_crash(self, delivery_tag: int):
        """Simulates worker crash before ack: unacked message is automatically requeued."""
        self.nack(delivery_tag, requeue=True)
