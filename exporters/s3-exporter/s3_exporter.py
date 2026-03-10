"""
Custom Prometheus exporter for S3-compatible storage (Garage / MinIO).

Exposes bucket-level metrics: object count, total size, and per-bucket
availability via the ListObjectsV2 API.

Environment variables:
    S3_ENDPOINT        – S3 endpoint URL  (e.g. http://garage:3900)
    S3_ACCESS_KEY      – Access key ID
    S3_SECRET_KEY      – Secret access key
    S3_BUCKET          – Comma-separated list of buckets (or "*" for all)
    S3_REGION          – Region name (default: garage)
    SCRAPE_INTERVAL    – Seconds between background collection cycles (default: 60)
    EXPORTER_PORT      – HTTP port to expose metrics on (default: 9340)
"""

import logging
import os
import signal
import sys
import threading
import time

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from prometheus_client import (
    CollectorRegistry,
    Gauge,
    Counter,
    Info,
    generate_latest,
    start_http_server,
)

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
log = logging.getLogger("s3-exporter")

# ── Configuration ────────────────────────────────────────────────────────────

S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://localhost:3900")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "*")
S3_REGION = os.environ.get("S3_REGION", "garage")
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", "60"))
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9340"))

# ── Prometheus metrics ───────────────────────────────────────────────────────

registry = CollectorRegistry()

bucket_objects_total = Gauge(
    "s3_bucket_objects_total",
    "Total number of objects in the bucket",
    ["bucket"],
    registry=registry,
)

bucket_size_bytes = Gauge(
    "s3_bucket_size_bytes",
    "Total size of all objects in the bucket in bytes",
    ["bucket"],
    registry=registry,
)

bucket_largest_object_bytes = Gauge(
    "s3_bucket_largest_object_bytes",
    "Size of the largest object in the bucket in bytes",
    ["bucket"],
    registry=registry,
)

bucket_up = Gauge(
    "s3_bucket_up",
    "Whether the bucket is reachable (1 = yes, 0 = no)",
    ["bucket"],
    registry=registry,
)

scrape_errors_total = Counter(
    "s3_exporter_scrape_errors_total",
    "Number of errors encountered during scraping",
    registry=registry,
)

scrape_duration_seconds = Gauge(
    "s3_exporter_scrape_duration_seconds",
    "Duration of the last scrape in seconds",
    registry=registry,
)

exporter_info = Info(
    "s3_exporter",
    "S3 exporter metadata",
    registry=registry,
)
exporter_info.info({"version": "1.0.0", "endpoint": S3_ENDPOINT})


# ── S3 client ────────────────────────────────────────────────────────────────

def _create_s3_client():
    """Create a boto3 S3 client pointing at the configured endpoint."""
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        config=BotoConfig(
            signature_version="s3v4",
            retries={"max_attempts": 2, "mode": "standard"},
            connect_timeout=5,
            read_timeout=10,
        ),
    )


def _discover_buckets(client) -> list[str]:
    """Return a list of bucket names to monitor."""
    if S3_BUCKET.strip() != "*":
        return [b.strip() for b in S3_BUCKET.split(",") if b.strip()]
    resp = client.list_buckets()
    return [b["Name"] for b in resp.get("Buckets", [])]


# ── Collection logic ─────────────────────────────────────────────────────────

def _collect_bucket_metrics(client, bucket: str):
    """Walk all objects in *bucket* and update Prometheus gauges."""
    total_objects = 0
    total_bytes = 0
    max_object_size = 0

    paginator = client.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                total_objects += 1
                size = obj.get("Size", 0)
                total_bytes += size
                if size > max_object_size:
                    max_object_size = size

        bucket_objects_total.labels(bucket=bucket).set(total_objects)
        bucket_size_bytes.labels(bucket=bucket).set(total_bytes)
        bucket_largest_object_bytes.labels(bucket=bucket).set(max_object_size)
        bucket_up.labels(bucket=bucket).set(1)

    except (BotoCoreError, ClientError) as exc:
        log.warning("Error collecting metrics for bucket '%s': %s", bucket, exc)
        bucket_up.labels(bucket=bucket).set(0)
        scrape_errors_total.inc()


def collect_all():
    """Run a full collection cycle across all configured buckets."""
    start = time.monotonic()
    client = _create_s3_client()
    try:
        buckets = _discover_buckets(client)
    except (BotoCoreError, ClientError) as exc:
        log.error("Failed to discover buckets: %s", exc)
        scrape_errors_total.inc()
        return

    for bucket in buckets:
        _collect_bucket_metrics(client, bucket)

    elapsed = time.monotonic() - start
    scrape_duration_seconds.set(elapsed)
    log.info(
        "Collection complete – %d bucket(s), %.2f s elapsed", len(buckets), elapsed
    )


# ── Background loop ─────────────────────────────────────────────────────────

_shutdown = threading.Event()


def _collection_loop():
    """Run collect_all() on a fixed interval until shutdown."""
    while not _shutdown.is_set():
        try:
            collect_all()
        except Exception:
            log.exception("Unexpected error during collection")
            scrape_errors_total.inc()
        _shutdown.wait(timeout=SCRAPE_INTERVAL)


def _handle_signal(signum, _frame):
    log.info("Received signal %s – shutting down", signum)
    _shutdown.set()


# ── Entrypoint ───────────────────────────────────────────────────────────────

def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info(
        "Starting S3 exporter on :%d  (endpoint=%s, interval=%ds)",
        EXPORTER_PORT,
        S3_ENDPOINT,
        SCRAPE_INTERVAL,
    )

    # Initial collection before exposing metrics endpoint
    collect_all()

    # Start Prometheus HTTP server
    start_http_server(EXPORTER_PORT, registry=registry)
    log.info("Metrics server listening on :%d/metrics", EXPORTER_PORT)

    # Background collection thread
    worker = threading.Thread(target=_collection_loop, daemon=True)
    worker.start()

    _shutdown.wait()
    log.info("Exporter shut down cleanly")
    sys.exit(0)


if __name__ == "__main__":
    main()
