#!/usr/bin/env bash
# run.sh -- drive the benchmark from one place, for the backend named by
# BACKEND in bench.conf (a plugin under backends/).
#
# usage: bin/run.sh [-c CONF] [-l ssh|slurm|manual] PHASE [PHASE...]
#
# Phases (run in the order given):
#   deploy       copy this directory to every node (ssh only; skipped where the
#                directory is already shared)
#   build        build the backend's writers on each server node
#   serve-write  start the write backend's service on the servers, if it has one
#   write        write the data set on the servers, once per writer in WRITERS,
#                and record a metadata file for each target
#   serve        start the query backend's service on the servers, if it has one
#                (skipped when it is the same service as serve-write)
#   check        run the query set from the query node, once per target
#   live         write a new target slowly while the query node polls it
#   stop         stop both backend services
#   clean        stop the services and delete the servers' data and run files
#   all          build serve-write write serve check stop
#
# Launchers:
#   ssh      run each step over ssh (default)
#   slurm    run each step with srun inside an allocation (see slurm/)
#   manual   print each step for you to run on the named node, then wait
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$(cd "${HERE}/.." && pwd)/bench.conf"
LAUNCHER="ssh"

while getopts "c:l:h" opt; do
	case "$opt" in
	c) CONF="$(cd "$(dirname "$OPTARG")" && pwd)/$(basename "$OPTARG")" ;;
	l) LAUNCHER="$OPTARG" ;;
	*) sed -n '2,26p' "$0"; exit 2 ;;
	esac
done
shift $((OPTIND - 1))
[[ $# -gt 0 ]] || { sed -n '2,26p' "$0"; exit 2; }
case "$LAUNCHER" in ssh | slurm | manual) ;; *) echo "unknown launcher '$LAUNCHER'" >&2; exit 2 ;; esac

# In the Slurm launcher, empty node settings are filled from the allocation.
if [[ "$LAUNCHER" == "slurm" ]]; then
	[[ -n "${SLURM_JOB_ID:-}" ]] || { echo "the slurm launcher must run inside an allocation (salloc/sbatch)" >&2; exit 1; }
	mapfile -t ALLOC < <(scontrol show hostnames "${SLURM_JOB_NODELIST}")
	if [[ -z "${SERVER_NODES:-}" ]]; then
		[[ ${#ALLOC[@]} -ge 3 ]] || { echo "need 3 nodes in the allocation, have ${#ALLOC[@]}" >&2; exit 1; }
		export SERVER_NODES="${ALLOC[0]} ${ALLOC[1]}"
		export QUERY_NODE="${QUERY_NODE:-${ALLOC[2]}}"
	fi
fi

# shellcheck source=../bench.conf
source "$CONF"

check_plugin() { # NAME
	[[ -f "${BENCH_DIR}/backends/$1/backend.conf" && -f "${BENCH_DIR}/backends/$1/plugin.sh" ]] ||
		{ echo "unknown backend '$1': no plugin in ${BENCH_DIR}/backends/$1" >&2; exit 1; }
}
check_plugin "${WRITE_BACKEND}"
check_plugin "${QUERY_BACKEND}"

# The write backend supplies the writers and their default settings.
# shellcheck source=../backends/sos/backend.conf
source "${BENCH_DIR}/backends/${WRITE_BACKEND}/backend.conf"
WRITERS="${WRITERS:-${DEFAULT_WRITERS:-}}"
LIVE_WRITER="${LIVE_WRITER:-${WRITERS%% *}}"
# Does the write backend require a service on the servers?
W_SERVICE=0
{
	PLUGIN_SERVICE=0
	source "${BENCH_DIR}/backends/${WRITE_BACKEND}/backend.conf"
	W_SERVICE="${PLUGIN_SERVICE:-0}"
} 2>/dev/null

# Does the query backend require a service, and what data can it read?
Q_SERVICE=0
Q_READS=""
{
	PLUGIN_SERVICE=0 PLUGIN_READS=""
	source "${BENCH_DIR}/backends/${QUERY_BACKEND}/backend.conf"
	Q_SERVICE="${PLUGIN_SERVICE:-0}"
	Q_READS="${PLUGIN_READS:-}"
} 2>/dev/null

# Re-source the write backend so its settings (not the query backend's) are
# the ones forwarded to the write nodes.
source "${BENCH_DIR}/backends/${WRITE_BACKEND}/backend.conf"

# The query backend must be able to read what the write backend produced.
if [[ -n "${Q_READS}" && " ${Q_READS} " != *" ${WRITE_BACKEND} "* ]]; then
	echo "backend '${QUERY_BACKEND}' cannot query data written by '${WRITE_BACKEND}' (it reads: ${Q_READS})" >&2
	exit 1
fi

read -r -a SERVERS <<<"${SERVER_NODES}"
[[ ${#SERVERS[@]} -ge 1 ]] || { echo "SERVER_NODES must list at least one host" >&2; exit 1; }
[[ -n "${WRITERS}" ]] || { echo "no WRITERS set and backend ${WRITE_BACKEND} has no DEFAULT_WRITERS" >&2; exit 1; }

# Settings forwarded to every remote step so that environment overrides given
# to run.sh apply on all nodes, not only here.
CONFIG_VARS=(BE WRITE_BACKEND QUERY_BACKEND SERVER_NODES QUERY_NODE BENCH_DIR RUN_DIR RESULTS_DIR
	ENV_SETUP PYTHON DATASET WRITERS DATA_NAME COMPONENTS_PER_NODE SAMPLES T0
	INTERVAL JOB_LEN BATCH QUERY_ADAPTER QUERY_FILE REPEAT LIMIT_N
	LIVE_WRITER LIVE_TARGET LIVE_RATE LIVE_BATCH LIVE_SAMPLES LIVE_POLL)
# Forward each plugin's own settings too, from both backends.
for _bc in "${WRITE_BACKEND}" "${QUERY_BACKEND}"; do
	_pv="$(PLUGIN_VARS=""; source "${BENCH_DIR}/backends/${_bc}/backend.conf" 2>/dev/null; echo "${PLUGIN_VARS:-}")"
	for _v in ${_pv}; do
		[[ " ${CONFIG_VARS[*]} " == *" ${_v} "* ]] || CONFIG_VARS+=("${_v}")
	done
done

STATE_DIR="${TMPDIR:-/tmp}/monbench-driver-${USER:-$(id -un)}"
LOG_DIR="${RESULTS_DIR}/logs"
mkdir -p "$STATE_DIR" "$LOG_DIR"
FAILED=0

say() { printf '\n== %s\n' "$*"; }

node_cmd() { # ACTION ARGS... -> command line to run on a node
	local assigns v
	assigns="env MONBENCH_CONF=$(printf '%q' "$CONF") "
	# In manual mode the command is typed by hand, so only the configuration
	# file is named; edit bench.conf rather than overriding from the shell.
	if [[ "$LAUNCHER" != "manual" ]]; then
		for v in "${CONFIG_VARS[@]}"; do
			# Skip variables that are unset; they inherit from bench.conf on the node.
			[[ -v "$v" ]] && assigns+="${v}=$(printf '%q' "${!v}") "
		done
	fi
	printf 'cd %q && %sbash bin/node.sh' "$BENCH_DIR" "$assigns"
	printf ' %q' "$@"
}

wait_for_user() {
	# Without a terminal (e.g. output redirected in a batch job) do not wait.
	{ read -r -p "   Press Enter when the step above has finished... " _ </dev/tty; } 2>/dev/null || true
}

# Run one node action and wait for it. Output goes to the terminal and to a
# log file under RESULTS_DIR/logs.
remote() { # HOST LOGNAME ACTION ARGS...
	local host="$1" logname="$2"
	shift 2
	local cmd
	cmd="$(node_cmd "$@")"
	case "$LAUNCHER" in
	ssh) ssh -n ${SSH_OPTS} "$host" "$cmd" 2>&1 | tee "${LOG_DIR}/${logname}.log"; return "${PIPESTATUS[0]}" ;;
	slurm) srun ${SRUN_OPTS} -N1 -n1 -w "$host" bash -c "$cmd" 2>&1 | tee "${LOG_DIR}/${logname}.log"; return "${PIPESTATUS[0]}" ;;
	manual)
		printf '   On %s run:\n     %s\n' "$host" "$cmd"
		wait_for_user
		;;
	esac
}

# Run the same action on every server at once and wait for all of them. The
# server's index and name are appended to the action's arguments.
on_servers() { # LOGPREFIX ACTION ARGS...
	local prefix="$1"
	shift
	local pids=() i rc=0
	if [[ "$LAUNCHER" == "manual" ]]; then
		echo "   Run these on the servers at the same time:"
		for i in "${!SERVERS[@]}"; do
			printf '   On %s:\n     %s\n' "${SERVERS[$i]}" "$(node_cmd "$@" "$i" "${SERVERS[$i]}")"
		done
		wait_for_user
		return 0
	fi
	for i in "${!SERVERS[@]}"; do
		remote "${SERVERS[$i]}" "${prefix}-${SERVERS[$i]}" "$@" "$i" "${SERVERS[$i]}" &
		pids+=($!)
	done
	for i in "${pids[@]}"; do
		wait "$i" || rc=1
	done
	return $rc
}

# ----------------------------------------------------------------- phases

phase_deploy() {
	[[ "$LAUNCHER" == "ssh" ]] || { echo "   deploy is only needed with the ssh launcher"; return 0; }
	local marker host id
	marker="${BENCH_DIR}/.monbench-id"
	[[ -f "$marker" ]] || od -An -N8 -tx8 /dev/urandom | tr -d ' ' >"$marker"
	id="$(cat "$marker")"
	for host in "${SERVERS[@]}" "${QUERY_NODE}"; do
		if [[ "$(ssh -n ${SSH_OPTS} "$host" cat "$(printf '%q' "$marker")" 2>/dev/null || true)" == "$id" ]]; then
			echo "   ${host}: directory is shared, nothing to copy"
			continue
		fi
		echo "   ${host}: copying ${BENCH_DIR}"
		ssh -n ${SSH_OPTS} "$host" mkdir -p "$(printf '%q' "$BENCH_DIR")"
		if command -v rsync >/dev/null; then
			rsync -a --exclude results -e "ssh ${SSH_OPTS}" "${BENCH_DIR}/" "${host}:${BENCH_DIR}/"
		else
			scp -rq ${SSH_OPTS} "${BENCH_DIR}/." "${host}:${BENCH_DIR}/"
		fi
	done
}

phase_build() {
	local host
	# Sequential, so that builds in a shared directory do not collide.
	for host in "${SERVERS[@]}"; do
		BE="${WRITE_BACKEND}" remote "$host" "build-${host}" build || FAILED=1
	done
}

summarise_writes() { # TARGET
	local f line host
	printf '   %-8s %-16s %10s %9s %9s %12s\n' writer host records write_s sync_s records/s
	for f in "${LOG_DIR}/write-$1-"*.log; do
		[[ -f "$f" ]] || continue
		host="$(basename "$f" .log | sed "s/^write-$1-//")"
		line="$(grep '^{' "$f" | tail -1 || true)"
		if [[ -z "$line" ]]; then
			printf '   %-8s %-16s %s\n' "?" "$host" "no summary; the write step failed (see $f)"
			continue
		fi
		printf '%s\n' "$line" | "${PYTHON}" -c '
import json, sys
d = json.loads(sys.stdin.read())
num = lambda k, f: (f % d[k]) if isinstance(d.get(k), (int, float)) else "-"
print("   %-8s %-16s %10s %9s %9s %12s" % (d.get("writer", "?"), sys.argv[1],
      num("records", "%d"), num("write_s", "%.2f"), num("close_sync_s", "%.2f"),
      num("records_per_s", "%d")))
' "$host" || true
	done
}

phase_write() {
	local w target
	for w in ${WRITERS}; do
		target="${DATA_NAME}_${w}"
		say "write: ${target} with the ${WRITE_BACKEND} ${w} writer on ${SERVERS[*]}"
		if BE="${WRITE_BACKEND}" on_servers "write-${target}" write "$w" "$target" full; then
			# Record what was written, where the query node will read it.
			BE="${WRITE_BACKEND}" remote "${QUERY_NODE}" "meta-${target}" meta "$target" "$w" full || FAILED=1
		else
			FAILED=1
		fi
		[[ "$LAUNCHER" == "manual" ]] || summarise_writes "$target"
	done
}

# Start one backend's service on all server nodes, identified by BACKEND_NAME
# and its SERVICE flag. The log/pid prefix distinguishes write from query.
_phase_serve_backend() { # BACKEND_NAME SERVICE_FLAG LOG_PREFIX
	local backend="$1" service="$2" prefix="$3"
	local host i
	if [[ "${service}" != "1" ]]; then
		echo "   ${backend} backend runs no service on the servers; nothing to start"
		return 0
	fi
	case "$LAUNCHER" in
	ssh | manual)
		for host in "${SERVERS[@]}"; do
			BE="${backend}" remote "$host" "${prefix}-${host}" serve-start "$host" || FAILED=1
		done
		;;
	slurm)
		# A background srun step keeps the service alive; its pid is kept here
		# so that 'stop' can end the step.
		for host in "${SERVERS[@]}"; do
			srun ${SRUN_OPTS} -N1 -n1 -w "$host" bash -c "$(BE="${backend}" node_cmd serve-run "$host")" \
				>"${LOG_DIR}/${prefix}-${host}.log" 2>&1 &
			echo $! >"${STATE_DIR}/srun-${prefix}-${host}.pid"
		done
		for host in "${SERVERS[@]}"; do
			for i in $(seq 1 30); do
				BE="${backend}" remote "$host" "${prefix}-status-${host}" serve-status >/dev/null 2>&1 && break
				sleep 2
			done
			BE="${backend}" remote "$host" "${prefix}-status-${host}" serve-status || FAILED=1
		done
		;;
	esac
}

# Stop one backend's service on all server nodes.
_phase_stop_backend() { # BACKEND_NAME SERVICE_FLAG LOG_PREFIX
	local backend="$1" service="$2" prefix="$3"
	local host pidfile
	[[ "${service}" == "1" ]] || return 0
	for host in "${SERVERS[@]}"; do
		pidfile="${STATE_DIR}/srun-${prefix}-${host}.pid"
		if [[ "$LAUNCHER" == "slurm" && -f "$pidfile" ]]; then
			kill "$(cat "$pidfile")" 2>/dev/null || true
			rm -f "$pidfile"
		fi
		BE="${backend}" remote "$host" "stop-${prefix}-${host}" serve-stop || true
	done
}

phase_write_serve() {
	say "serve-write: starting ${WRITE_BACKEND} service on ${SERVERS[*]}"
	_phase_serve_backend "${WRITE_BACKEND}" "${W_SERVICE}" "serve-write"
}

phase_serve() {
	# When write and query backends are the same, their service was already
	# started by serve-write; running serve-start again would be a no-op at
	# best, so skip it.
	if [[ "${WRITE_BACKEND}" == "${QUERY_BACKEND}" ]]; then
		echo "   write and query backend are both '${QUERY_BACKEND}'; service already started by serve-write"
		return 0
	fi
	_phase_serve_backend "${QUERY_BACKEND}" "${Q_SERVICE}" "serve"
}

phase_check() {
	local w target
	for w in ${WRITERS}; do
		target="${DATA_NAME}_${w}"
		say "check: ${target} from ${QUERY_NODE}"
		BE="${QUERY_BACKEND}" remote "${QUERY_NODE}" "check-${target}" check "$target" || FAILED=1
	done
}

phase_live() {
	say "live: ${LIVE_WRITER} writer at ${LIVE_RATE} records/s per server while ${QUERY_NODE} polls"
	if [[ "$LAUNCHER" == "manual" ]]; then
		on_servers "write-${LIVE_TARGET}" write "${LIVE_WRITER}" "${LIVE_TARGET}" live
		BE="${QUERY_BACKEND}" remote "${QUERY_NODE}" "check-live" check "${LIVE_TARGET}" live
		return 0
	fi
	BE="${WRITE_BACKEND}" on_servers "write-${LIVE_TARGET}" write "${LIVE_WRITER}" "${LIVE_TARGET}" live &
	local writers_pid=$!
	sleep 3
	BE="${QUERY_BACKEND}" remote "${QUERY_NODE}" "check-live" check "${LIVE_TARGET}" live || FAILED=1
	wait "$writers_pid" || FAILED=1
}

phase_stop() {
	# Stop the query backend's service first (it may depend on the write
	# backend's data), then the write backend's. When both are the same,
	# the second call is a no-op because the process is already gone.
	_phase_stop_backend "${QUERY_BACKEND}" "${Q_SERVICE}" "serve"
	if [[ "${WRITE_BACKEND}" != "${QUERY_BACKEND}" ]]; then
		_phase_stop_backend "${WRITE_BACKEND}" "${W_SERVICE}" "serve-write"
	fi
}

phase_clean() {
	phase_stop
	local host
	for host in "${SERVERS[@]}"; do
		BE="${WRITE_BACKEND}" remote "$host" "clean-${host}" clean "$host" || true
	done
}

echo "monbench: write=${WRITE_BACKEND} query=${QUERY_BACKEND} launcher=${LAUNCHER} servers=${SERVERS[*]} query-node=${QUERY_NODE}"
echo "          ${COMPONENTS_PER_NODE} components x ${SAMPLES} samples per server, writers: ${WRITERS}"
PHASES=()
for phase in "$@"; do
	if [[ "$phase" == "all" ]]; then
		PHASES+=(build serve-write write serve check stop)
	else
		PHASES+=("$phase")
	fi
done
for phase in "${PHASES[@]}"; do
	say "phase: ${phase}"
	case "$phase" in
	deploy) phase_deploy ;;
	build) phase_build || FAILED=1 ;;
	serve-write) phase_write_serve ;;
	write) phase_write ;;
	serve) phase_serve ;;
	check) phase_check ;;
	live) phase_live ;;
	stop) phase_stop ;;
	clean) phase_clean ;;
	*) echo "unknown phase '$phase'" >&2; exit 2 ;;
	esac
done

say "done: $([[ $FAILED -eq 0 ]] && echo 'all steps succeeded' || echo 'one or more steps FAILED')"
echo "   logs and results: ${RESULTS_DIR}"
exit $FAILED
