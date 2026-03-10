"""
Custom Prometheus exporter for Apache Tomcat.

Scrapes the Tomcat server-status page (XML format) and exposes metrics for
memory, threads, request processing, and connector statistics.

Environment variables:
    TOMCAT_URL          – Base URL of Tomcat (e.g. http://tomcat:8080)
    TOMCAT_STATUS_PATH  – Path to XML status page (default: /manager/status/all?XML=true)
    TOMCAT_USER         – Manager user
    TOMCAT_PASSWORD     – Manager password
    SCRAPE_INTERVAL     – Seconds between background collection cycles (default: 30)
    EXPORTER_PORT       – HTTP port to expose metrics on (default: 9341)
"""

import logging
import os
import signal
import sys
import threading
import time
import xml.etree.ElementTree as ET

import requests
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Info,
    generate_latest,
    start_http_server,
)

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
log = logging.getLogger("tomcat-exporter")

# ── Configuration ────────────────────────────────────────────────────────────

TOMCAT_URL = os.environ.get("TOMCAT_URL", "http://localhost:8080")
TOMCAT_STATUS_PATH = os.environ.get(
    "TOMCAT_STATUS_PATH", "/manager/status/all?XML=true"
)
TOMCAT_USER = os.environ.get("TOMCAT_USER", "admin")
TOMCAT_PASSWORD = os.environ.get("TOMCAT_PASSWORD", "admin")
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", "30"))
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9341"))

# ── Prometheus metrics ───────────────────────────────────────────────────────

registry = CollectorRegistry()

# JVM memory
jvm_memory_free_bytes = Gauge(
    "tomcat_jvm_memory_free_bytes",
    "JVM free memory in bytes",
    registry=registry,
)
jvm_memory_total_bytes = Gauge(
    "tomcat_jvm_memory_total_bytes",
    "JVM total memory in bytes",
    registry=registry,
)
jvm_memory_max_bytes = Gauge(
    "tomcat_jvm_memory_max_bytes",
    "JVM max memory in bytes",
    registry=registry,
)

# Memory pools
memory_pool_usage_bytes = Gauge(
    "tomcat_memory_pool_usage_bytes",
    "Memory pool current utilization in bytes",
    ["pool", "type"],
    registry=registry,
)
memory_pool_max_bytes = Gauge(
    "tomcat_memory_pool_max_bytes",
    "Memory pool max in bytes",
    ["pool", "type"],
    registry=registry,
)

# Thread pool / connectors
connector_thread_max = Gauge(
    "tomcat_connector_thread_max",
    "Maximum threads for the connector",
    ["connector"],
    registry=registry,
)
connector_thread_count = Gauge(
    "tomcat_connector_thread_count",
    "Current thread count for the connector",
    ["connector"],
    registry=registry,
)
connector_thread_busy = Gauge(
    "tomcat_connector_thread_busy",
    "Current busy threads for the connector",
    ["connector"],
    registry=registry,
)
connector_max_connections = Gauge(
    "tomcat_connector_max_connections",
    "Maximum connections for the connector",
    ["connector"],
    registry=registry,
)
connector_connection_count = Gauge(
    "tomcat_connector_connection_count",
    "Current connection count for the connector",
    ["connector"],
    registry=registry,
)

# Request processor statistics
connector_request_count_total = Gauge(
    "tomcat_connector_request_count_total",
    "Total number of requests processed",
    ["connector"],
    registry=registry,
)
connector_error_count_total = Gauge(
    "tomcat_connector_error_count_total",
    "Total number of errors",
    ["connector"],
    registry=registry,
)
connector_bytes_received_total = Gauge(
    "tomcat_connector_bytes_received_total",
    "Total bytes received",
    ["connector"],
    registry=registry,
)
connector_bytes_sent_total = Gauge(
    "tomcat_connector_bytes_sent_total",
    "Total bytes sent",
    ["connector"],
    registry=registry,
)
connector_processing_time_ms = Gauge(
    "tomcat_connector_processing_time_ms_total",
    "Total processing time in milliseconds",
    ["connector"],
    registry=registry,
)
connector_max_time_ms = Gauge(
    "tomcat_connector_max_time_ms",
    "Maximum processing time for a single request in ms",
    ["connector"],
    registry=registry,
)

# Exporter meta
tomcat_up = Gauge(
    "tomcat_up",
    "Whether Tomcat status page is reachable (1 = yes, 0 = no)",
    registry=registry,
)
scrape_errors_total = Counter(
    "tomcat_exporter_scrape_errors_total",
    "Number of errors encountered during scraping",
    registry=registry,
)
scrape_duration_seconds = Gauge(
    "tomcat_exporter_scrape_duration_seconds",
    "Duration of the last scrape in seconds",
    registry=registry,
)
exporter_info = Info(
    "tomcat_exporter",
    "Tomcat exporter metadata",
    registry=registry,
)
exporter_info.info({"version": "1.0.0", "target": TOMCAT_URL})


# ── XML parsing ──────────────────────────────────────────────────────────────

def _safe_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_status(xml_text: str):
    """Parse Tomcat /status XML and update Prometheus gauges."""
    root = ET.fromstring(xml_text)

    # ── JVM memory ──
    jvm = root.find("jvm/memory")
    if jvm is not None:
        jvm_memory_free_bytes.set(_safe_int(jvm.get("free")))
        jvm_memory_total_bytes.set(_safe_int(jvm.get("total")))
        jvm_memory_max_bytes.set(_safe_int(jvm.get("max")))

    # ── Memory pools ──
    for pool in root.findall("jvm/memorypool"):
        name = pool.get("name", "unknown")
        mtype = pool.get("type", "unknown")
        memory_pool_usage_bytes.labels(pool=name, type=mtype).set(
            _safe_int(pool.get("usageUsed"))
        )
        memory_pool_max_bytes.labels(pool=name, type=mtype).set(
            _safe_int(pool.get("usageMax"))
        )

    # ── Connectors ──
    for conn in root.findall("connector"):
        name = conn.get("name", "unknown")

        # Thread info
        tp = conn.find("threadInfo")
        if tp is not None:
            connector_thread_max.labels(connector=name).set(
                _safe_int(tp.get("maxThreads"))
            )
            connector_thread_count.labels(connector=name).set(
                _safe_int(tp.get("currentThreadCount"))
            )
            connector_thread_busy.labels(connector=name).set(
                _safe_int(tp.get("currentThreadsBusy"))
            )

        # Request info (aggregated)
        rp = conn.find("requestInfo")
        if rp is not None:
            connector_request_count_total.labels(connector=name).set(
                _safe_int(rp.get("requestCount"))
            )
            connector_error_count_total.labels(connector=name).set(
                _safe_int(rp.get("errorCount"))
            )
            connector_bytes_received_total.labels(connector=name).set(
                _safe_int(rp.get("bytesReceived"))
            )
            connector_bytes_sent_total.labels(connector=name).set(
                _safe_int(rp.get("bytesSent"))
            )
            connector_processing_time_ms.labels(connector=name).set(
                _safe_int(rp.get("processingTime"))
            )
            connector_max_time_ms.labels(connector=name).set(
                _safe_int(rp.get("maxTime"))
            )


# ── Collection logic ─────────────────────────────────────────────────────────

def collect_all():
    """Fetch Tomcat status page and update metrics."""
    start = time.monotonic()
    url = f"{TOMCAT_URL.rstrip('/')}{TOMCAT_STATUS_PATH}"

    try:
        resp = requests.get(url, auth=(TOMCAT_USER, TOMCAT_PASSWORD), timeout=10)
        resp.raise_for_status()
        _parse_status(resp.text)
        tomcat_up.set(1)
    except requests.RequestException as exc:
        log.warning("Failed to fetch Tomcat status: %s", exc)
        tomcat_up.set(0)
        scrape_errors_total.inc()
    except ET.ParseError as exc:
        log.warning("Failed to parse Tomcat status XML: %s", exc)
        tomcat_up.set(0)
        scrape_errors_total.inc()

    elapsed = time.monotonic() - start
    scrape_duration_seconds.set(elapsed)
    log.info("Collection complete – %.3f s elapsed", elapsed)


# ── Background loop ─────────────────────────────────────────────────────────

_shutdown = threading.Event()


def _collection_loop():
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
        "Starting Tomcat exporter on :%d  (target=%s, interval=%ds)",
        EXPORTER_PORT,
        TOMCAT_URL,
        SCRAPE_INTERVAL,
    )

    collect_all()

    start_http_server(EXPORTER_PORT, registry=registry)
    log.info("Metrics server listening on :%d/metrics", EXPORTER_PORT)

    worker = threading.Thread(target=_collection_loop, daemon=True)
    worker.start()

    _shutdown.wait()
    log.info("Exporter shut down cleanly")
    sys.exit(0)


if __name__ == "__main__":
    main()
