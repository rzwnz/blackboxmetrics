#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# dump-metrics.sh — Export a time-window of VictoriaMetrics TSDB data and
#                   application logs into a compressed tar.gz archive.
#
# Usage:
#   ./dump-metrics.sh                        # last 24 hours
#   ./dump-metrics.sh "2025-01-01" "2025-01-07"  # specific range
#   LOGS_DIR=/opt/tomcat/logs ./dump-metrics.sh   # custom log path
#
# Requires: curl, tar, gzip (and optionally pigz for faster compression)
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Configuration (override via env vars) ────────────────────────────────────

VM_URL="${VM_URL:-http://localhost:8428}"
LOGS_DIR="${LOGS_DIR:-/var/log/tandem}"
OUTPUT_DIR="${OUTPUT_DIR:-./dumps}"
DUMP_PREFIX="${DUMP_PREFIX:-blackbox-dump}"

# Time range — default: last 24 hours
START_DATE="${1:-$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -u -v-24H +%Y-%m-%dT%H:%M:%S)}"
END_DATE="${2:-$(date -u +%Y-%m-%dT%H:%M:%S)}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
WORK_DIR=$(mktemp -d "/tmp/${DUMP_PREFIX}-XXXXXX")

trap 'rm -rf "${WORK_DIR}"' EXIT

# ── Helpers ──────────────────────────────────────────────────────────────────

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

check_prereqs() {
    for cmd in curl tar; do
        if ! command -v "$cmd" &>/dev/null; then
            log "ERROR: required command '$cmd' not found"
            exit 1
        fi
    done
}

# ── Step 1: Export VictoriaMetrics data ──────────────────────────────────────

export_metrics() {
    local out_file="${WORK_DIR}/metrics-export.jsonl"

    log "Exporting VictoriaMetrics data from ${START_DATE} to ${END_DATE}..."

    # Use /api/v1/export which returns JSONL (one JSON line per time-series)
    local http_code
    http_code=$(curl -s -o "${out_file}" -w "%{http_code}" \
        "${VM_URL}/api/v1/export" \
        --data-urlencode "match[]={__name__!=\"\"}" \
        --data-urlencode "start=${START_DATE}" \
        --data-urlencode "end=${END_DATE}")

    if [[ "${http_code}" -ne 200 ]]; then
        log "WARNING: VictoriaMetrics export returned HTTP ${http_code}"
        # Create empty file so the archive still works
        : > "${out_file}"
    fi

    local size
    size=$(wc -c < "${out_file}" | tr -d ' ')
    log "Metrics export: ${size} bytes"
}

# ── Step 2: Collect application logs ─────────────────────────────────────────

collect_logs() {
    local logs_out="${WORK_DIR}/logs"
    mkdir -p "${logs_out}"

    if [[ -d "${LOGS_DIR}" ]]; then
        log "Collecting logs from ${LOGS_DIR}..."
        # Copy only files modified within the time window (rough filter by mtime)
        find "${LOGS_DIR}" -type f \( -name '*.log' -o -name '*.txt' -o -name '*.gz' \) \
            -newermt "${START_DATE}" ! -newermt "${END_DATE}" \
            -exec cp --parents {} "${logs_out}/" \; 2>/dev/null || \
        # Fallback: copy all recent logs if date filtering not supported
        find "${LOGS_DIR}" -type f \( -name '*.log' -o -name '*.txt' \) -mtime -1 \
            -exec cp --parents {} "${logs_out}/" \; 2>/dev/null || true

        local count
        count=$(find "${logs_out}" -type f | wc -l)
        log "Collected ${count} log file(s)"
    else
        log "WARNING: Logs directory ${LOGS_DIR} does not exist, skipping"
    fi
}

# ── Step 3: Add metadata ────────────────────────────────────────────────────

add_metadata() {
    cat > "${WORK_DIR}/dump-metadata.json" <<EOF
{
    "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "hostname": "$(hostname)",
    "time_range": {
        "start": "${START_DATE}",
        "end": "${END_DATE}"
    },
    "vm_url": "${VM_URL}",
    "logs_dir": "${LOGS_DIR}",
    "vm_version": "$(curl -sf "${VM_URL}/api/v1/status/buildinfo" 2>/dev/null | head -c 500 || echo 'unknown')"
}
EOF
}

# ── Step 4: Compress into archive ───────────────────────────────────────────

create_archive() {
    mkdir -p "${OUTPUT_DIR}"
    local archive_name="${DUMP_PREFIX}-${TIMESTAMP}.tar.gz"
    local archive_path="${OUTPUT_DIR}/${archive_name}"

    log "Creating archive ${archive_path}..."

    # Use pigz if available for faster compression
    if command -v pigz &>/dev/null; then
        tar -cf - -C "${WORK_DIR}" . | pigz -9 > "${archive_path}"
    else
        tar -czf "${archive_path}" -C "${WORK_DIR}" .
    fi

    local size
    size=$(du -h "${archive_path}" | cut -f1)
    log "Archive created: ${archive_path} (${size})"

    # Generate SHA-256 checksum
    sha256sum "${archive_path}" > "${archive_path}.sha256"
    log "Checksum:  ${archive_path}.sha256"
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
    log "=== Black Box Metrics Dump ==="
    log "Time range: ${START_DATE} → ${END_DATE}"

    check_prereqs
    export_metrics
    collect_logs
    add_metadata
    create_archive

    log "=== Dump complete ==="
}

main
