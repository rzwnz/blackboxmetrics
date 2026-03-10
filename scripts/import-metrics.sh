#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# import-metrics.sh — Import a dump archive into the local VictoriaMetrics
#                     instance for offline analysis via Grafana.
#
# Usage:
#   ./import-metrics.sh ./dumps/blackbox-dump-20250101_120000.tar.gz
#
# Prerequisites: The analysis stack must be running:
#   docker compose -f docker-compose.analysis.yml up -d
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

VM_URL="${VM_URL:-http://localhost:8428}"
ARCHIVE="${1:?Usage: $0 <archive.tar.gz>}"
WORK_DIR=$(mktemp -d "/tmp/bb-import-XXXXXX")

trap 'rm -rf "${WORK_DIR}"' EXIT

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# ── Step 1: Verify archive ──────────────────────────────────────────────────

verify_archive() {
    if [[ ! -f "${ARCHIVE}" ]]; then
        log "ERROR: Archive not found: ${ARCHIVE}"
        exit 1
    fi

    # Check SHA-256 if checksum file exists
    local sha_file="${ARCHIVE}.sha256"
    if [[ -f "${sha_file}" ]]; then
        log "Verifying SHA-256 checksum..."
        if sha256sum -c "${sha_file}" --quiet 2>/dev/null; then
            log "Checksum OK"
        else
            log "WARNING: Checksum verification failed"
            read -rp "Continue anyway? [y/N] " answer
            [[ "${answer}" =~ ^[Yy]$ ]] || exit 1
        fi
    fi
}

# ── Step 2: Extract archive ─────────────────────────────────────────────────

extract_archive() {
    log "Extracting archive..."
    tar -xzf "${ARCHIVE}" -C "${WORK_DIR}"

    if [[ -f "${WORK_DIR}/dump-metadata.json" ]]; then
        log "Dump metadata:"
        cat "${WORK_DIR}/dump-metadata.json"
        echo
    fi
}

# ── Step 3: Import metrics into VictoriaMetrics ─────────────────────────────

import_metrics() {
    local metrics_file="${WORK_DIR}/metrics-export.jsonl"

    if [[ ! -f "${metrics_file}" ]]; then
        log "WARNING: No metrics file found in archive, skipping metric import"
        return
    fi

    local lines
    lines=$(wc -l < "${metrics_file}" | tr -d ' ')
    log "Importing ${lines} time-series into VictoriaMetrics at ${VM_URL}..."

    # Use /api/v1/import which accepts the same JSONL format produced by /export
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST \
        -H "Content-Type: application/json" \
        --data-binary "@${metrics_file}" \
        "${VM_URL}/api/v1/import")

    if [[ "${http_code}" -eq 204 || "${http_code}" -eq 200 ]]; then
        log "Metrics imported successfully (HTTP ${http_code})"
    else
        log "WARNING: Import returned HTTP ${http_code}"
    fi
}

# ── Step 4: Copy logs for viewing ───────────────────────────────────────────

copy_logs() {
    local logs_dir="${WORK_DIR}/logs"
    local import_logs="./import/logs"

    if [[ ! -d "${logs_dir}" ]] || [[ -z "$(ls -A "${logs_dir}" 2>/dev/null)" ]]; then
        log "No logs in the archive"
        return
    fi

    mkdir -p "${import_logs}"
    cp -r "${logs_dir}/." "${import_logs}/"

    local count
    count=$(find "${import_logs}" -type f | wc -l)
    log "Copied ${count} log file(s) to ${import_logs}"
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
    log "=== Black Box Metrics Import ==="
    log "Archive: ${ARCHIVE}"

    verify_archive
    extract_archive
    import_metrics
    copy_logs

    log "=== Import complete ==="
    log ""
    log "Open Grafana at http://localhost:3000 (admin/admin) to analyze."
}

main
