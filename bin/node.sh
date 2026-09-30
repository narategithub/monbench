#!/usr/bin/env bash
# node.sh -- actions executed on one node. bin/run.sh calls this script
# through ssh, srun, or by printing the command for you to run by hand.
# Backend-specific work is delegated to backends/<BACKEND>/<BACKEND>_plugin.sh.
#
#   node.sh build                                  build the backend's writers
#   node.sh write WRITER TARGET full|live INDEX ID write data-set partition INDEX
#                                                  into TARGET (server node)
#   node.sh meta TARGET WRITER full|live           record what was written
#   node.sh serve-start ID | serve-run ID          start the backend service
#   node.sh serve-stop | serve-status              stop / check it
#   node.sh check TARGET [live]                    run the query set (query node)
#   node.sh clean ID                               delete this server's data
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${MONBENCH_CONF:-${HERE}/../bench.conf}"
set -a
# shellcheck source=../bench.conf
source "$CONF"
# The backend to use for this call is passed by bin/run.sh as BE: the write
# backend for build/write/clean, the query backend for serve/query. It selects
# the plugin to load. Falls back to WRITE_BACKEND when not set.
BACKEND="${BE:-${BACKEND:-${WRITE_BACKEND}}}"
PLUGIN_DIR="${BENCH_DIR}/backends/${BACKEND}"
[[ -f "${PLUGIN_DIR}/${BACKEND}_plugin.sh" ]] || { echo "error: unknown backend '${BACKEND}' (no ${PLUGIN_DIR}/${BACKEND}_plugin.sh)" >&2; exit 1; }
source "${PLUGIN_DIR}/${BACKEND}_backend.conf"
WRITERS="${WRITERS:-${DEFAULT_WRITERS:-}}"
set +a
# Site profile scripts are written for interactive shells and often reference
# unset variables or run commands that can fail, so they are evaluated without
# this script's strict error settings.
if [[ -n "${ENV_SETUP:-}" ]]; then
	set +eu
	eval "${ENV_SETUP}"
	set -eu
fi

HOST="$(hostname -s)"
die() { echo "[${HOST}] error: $*" >&2; exit 1; }
log() { echo "[${HOST}] $*"; }

# Every target the harness writes: one per writer, plus the live-test target.
all_targets() {
	local w
	for w in ${WRITERS}; do
		echo "${DATA_NAME}_${w}"
	done
	echo "${LIVE_TARGET:-}"
}

# shellcheck source=../backends/sos/sos_plugin.sh
source "${PLUGIN_DIR}/${BACKEND}_plugin.sh"

# Run a plugin hook if the backend defines it.
hook() { # NAME ARGS...
	local fn="plugin_$1"
	shift
	if declare -F "$fn" >/dev/null; then
		"$fn" "$@"
	else
		log "backend ${BACKEND} has no '${fn#plugin_}' step; skipped"
	fi
}

cmd_write() { # WRITER TARGET MODE INDEX ID
	local target="$2" mode="$3" out
	local samples="${SAMPLES:-}" batch="${BATCH:-}" rate=0
	if [[ "$mode" == "live" ]]; then
		samples="${LIVE_SAMPLES:-}" batch="${LIVE_BATCH:-}" rate="${LIVE_RATE:-}"
	fi
	# Data-set arguments every writer accepts (see backends/README.md).
	dataset_args=(--components "${COMPONENTS_PER_NODE:-}" --samples "$samples"
		--t0 "${T0:-}" --interval "${INTERVAL:-}" --job-len "${JOB_LEN:-}"
		--batch "$batch" --rate "$rate")
	mkdir -p "${RESULTS_DIR:-}"
	out="${RESULTS_DIR}/write-${target}-${HOST}.json"
	local rc=0
	# With pipefail, the pipeline fails when the writer fails.
	hook write "$@" | tee "$out.tmp" || rc=1
	# Keep only the writer's JSON summary line.
	grep '^{' "$out.tmp" >"$out" || true
	rm -f "$out.tmp"
	return "$rc"
}

cmd_check() { # TARGET [live]
	local args=(--target "$1")
	[[ "${2:-}" == "live" ]] && args+=(--live)
	"${PYTHON:-}" "${BENCH_DIR}/core/bench.py" "${args[@]}"
}

action="${1:-}"
shift || true
case "$action" in
build) hook build ;;
write) cmd_write "$@" ;;
meta) "${PYTHON:-}" "${BENCH_DIR}/core/dataset.py" meta "$@" ;;
serve-start) hook serve_start "$@" ;;
serve-run) hook serve_run "$@" ;;
serve-stop) hook serve_stop ;;
serve-status) hook serve_status ;;
check) cmd_check "$@" ;;
clean) hook clean "$@" ;;
*) sed -n '2,14p' "$0"; exit 2 ;;
esac
