#!/usr/bin/env bash
# run.sh -- drive the benchmark from one place, for the write/query backend
# pair(s) named in bench.conf (plugins under backends/).
#
# usage: bin/run.sh [-c CONF] [-l ssh|slurm|manual] PHASE [PHASE...]
#
# Phases (run in the order given):
#   deploy       copy this directory to every node (ssh only; skipped where the
#                directory is already shared) -- runs once, before any pair
#   build        build the backend's writers on each server node
#   serve-write  start the write backend's service on the servers, if it has one
#   write        write the data set on the servers, once per writer in WRITERS,
#                and record a metadata file for each target
#   serve-query  start the query backend's service on the servers, if it has one
#                (skipped when it is the same service as serve-write)
#   check        verify query results from the query node, once per target
#                (no timing)
#   query        time the query set from the query node, once per target
#                (no verification)
#   live         write a new target slowly while the query node polls it
#   stop         stop both backend services
#   clean        stop the services and delete the servers' data and run files
#   all          build serve-write write serve-query check query stop
#   all-nocheck  build serve-write write serve-query query stop
#
# Launchers:
#   ssh      run each step over ssh (default)
#   slurm    run each step with srun inside an allocation (see slurm/)
#   manual   print each step for you to run on the named node, then wait
#
# Backend pairs:
#   bench.conf names either one pair (WRITE_BACKEND/QUERY_BACKEND) or several,
#   via BACKENDS -- a space-separated list of "writer" or "writer:query"
#   tokens (query defaults to writer), e.g.:
#     BACKENDS="sos:dsos influxdb"
#   All pairs share this file's SERVER_HOSTS, QUERY_HOST, DATASET, QUERY_FILE,
#   and every other setting; only WRITE_BACKEND/QUERY_BACKEND change per
#   pair. Every phase given on the command line runs for each pair in turn,
#   in the order listed, before moving to the next pair (deploy excepted --
#   it runs once, since it does not depend on the backend).
#
#   RUN_DIR and RESULTS_DIR (and so each backend's on-disk data -- see e.g.
#   SOS_DATA_ROOT, INFLUX_DATA_DIR) are both namespaced per pair, named after
#   it (e.g. "sos-dsos", "influxdb"). That name is fixed once per pair (see
#   run_pair below) and reused by every phase in it -- build, write, check,
#   query, ... -- regardless of which single backend (the write backend, or
#   the query backend) is actually doing the work for a given phase. That is
#   what keeps a pair's write and query sides agreeing on where the data is:
#   e.g. for "sos:dsos", the write phase (handled by sos) and the check/query
#   phases (handled by dsos, reading the very same on-disk containers sos
#   wrote) both resolve RUN_DIR from the one fixed "sos-dsos" pair name,
#   rather than from whichever single backend happens to be running that
#   particular phase -- keying it that way (e.g. "sos" while writing, "dsos"
#   while querying) is what caused them to disagree before.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$(cd "${HERE}/.." && pwd)/bench.conf"
LAUNCHER="ssh"

while getopts "c:l:h" opt; do
	case "$opt" in
	c) CONF="$(cd "$(dirname "$OPTARG")" && pwd)/$(basename "$OPTARG")" ;;
	l) LAUNCHER="$OPTARG" ;;
	*) sed -n '2,54p' "$0"; exit 2 ;;
	esac
done
shift $((OPTIND - 1))
[[ $# -gt 0 ]] || { sed -n '2,54p' "$0"; exit 2; }
case "$LAUNCHER" in ssh | slurm | manual) ;; *) echo "unknown launcher '$LAUNCHER'" >&2; exit 2 ;; esac

# In the Slurm launcher, empty host settings are filled from the allocation.
if [[ "$LAUNCHER" == "slurm" ]]; then
	[[ -n "${SLURM_JOB_ID:-}" ]] || { echo "the slurm launcher must run inside an allocation (salloc/sbatch)" >&2; exit 1; }
	mapfile -t ALLOC < <(scontrol show hostnames "${SLURM_JOB_NODELIST}")
	if [[ -z "${SERVER_HOSTS:-}" ]]; then
		[[ ${#ALLOC[@]} -ge 3 ]] || { echo "need 3 nodes in the allocation, have ${#ALLOC[@]}" >&2; exit 1; }
		export SERVER_HOSTS="${ALLOC[0]} ${ALLOC[1]}"
		export QUERY_HOST="${QUERY_HOST:-${ALLOC[2]}}"
	fi
fi

# shellcheck source=../bench.conf
source "$CONF"

# ------------------------------------------------------------ backend pairs
# BACKENDS, if set, lists every write/query pair to run this invocation, as
# space-separated "writer" or "writer:query" tokens (query defaults to
# writer). Otherwise there is exactly one pair, from WRITE_BACKEND/
# QUERY_BACKEND, so a single-pair bench.conf works unchanged.
PAIRS=()
if [[ -n "${BACKENDS:-}" ]]; then
	read -r -a _tokens <<<"${BACKENDS}"
	for _tok in "${_tokens[@]}"; do
		_w="${_tok%%:*}"
		if [[ "$_tok" == *:* ]]; then _q="${_tok#*:}"; else _q="$_w"; fi
		PAIRS+=("${_w}:${_q}")
	done
else
	PAIRS=("${WRITE_BACKEND:-}:${QUERY_BACKEND:-}")
fi
[[ ${#PAIRS[@]} -gt 0 ]] || { echo "BACKENDS is set but names no pairs (see bench.conf.example)" >&2; exit 1; }
for _chk in "${PAIRS[@]}"; do
	[[ -n "${_chk%%:*}" && -n "${_chk#*:}" ]] ||
		{ echo "no backend configured: set BACKENDS, or WRITE_BACKEND/QUERY_BACKEND, in bench.conf (see bench.conf.example)" >&2; exit 1; }
done

# RUN_DIR and RESULTS_DIR are both namespaced per pair below (see run_pair).
BASE_RUN_DIR="${RUN_DIR}"
BASE_RESULTS_DIR="${RESULTS_DIR}"

[[ -n "${SERVER_HOSTS:-}" ]] || { echo "SERVER_HOSTS must be set in bench.conf (see bench.conf.example)" >&2; exit 1; }
[[ -n "${QUERY_HOST:-}" ]] || { echo "QUERY_HOST must be set in bench.conf (see bench.conf.example)" >&2; exit 1; }
read -r -a SERVERS <<<"${SERVER_HOSTS}"
[[ ${#SERVERS[@]} -ge 1 ]] || { echo "SERVER_HOSTS must list at least one host" >&2; exit 1; }

check_plugin() { # NAME
	[[ -f "${BENCH_DIR}/backends/$1/${1}_backend.conf" && -f "${BENCH_DIR}/backends/$1/${1}_plugin.sh" ]] ||
		{ echo "unknown backend '$1': no plugin in ${BENCH_DIR}/backends/$1" >&2; exit 1; }
}

STATE_DIR="${TMPDIR:-/tmp}/monbench-driver-${USER:-$(id -un)}"
mkdir -p "$STATE_DIR"
OVERALL_FAILED=0

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
# Backend-independent except for reading the current WRITE_BACKEND/
# QUERY_BACKEND/PAIR_NAME globals, which run_pair sets before calling these.

phase_deploy() {
	[[ "$LAUNCHER" == "ssh" ]] || { echo "   deploy is only needed with the ssh launcher"; return 0; }
	local marker host id
	marker="${BENCH_DIR}/.monbench-id"
	[[ -f "$marker" ]] || od -An -N8 -tx8 /dev/urandom | tr -d ' ' >"$marker"
	id="$(cat "$marker")"
	for host in "${SERVERS[@]}" "${QUERY_HOST}"; do
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

phase_write() {
	local w target
	for w in ${WRITERS}; do
		target="${DATA_NAME}_${w}"
		say "write: ${target} with the ${WRITE_BACKEND} ${w} writer on ${SERVERS[*]}"
		if BE="${WRITE_BACKEND}" on_servers "write-${target}" write "$w" "$target" full; then
			# Record what was written, where the query node will read it.
			BE="${WRITE_BACKEND}" remote "${QUERY_HOST}" "meta-${target}" meta "$target" "$w" full || FAILED=1
		else
			FAILED=1
		fi
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
		# so that 'stop' can end the step. Namespaced by pair so two pairs
		# using the same host/prefix in one invocation don't clash.
		for host in "${SERVERS[@]}"; do
			srun ${SRUN_OPTS} -N1 -n1 -w "$host" bash -c "$(BE="${backend}" node_cmd serve-run "$host")" \
				>"${LOG_DIR}/${prefix}-${host}.log" 2>&1 &
			echo $! >"${STATE_DIR}/srun-${PAIR_NAME}-${prefix}-${host}.pid"
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
		pidfile="${STATE_DIR}/srun-${PAIR_NAME}-${prefix}-${host}.pid"
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

phase_query_serve() {
	# When write and query backend are the same, their service was already
	# started by serve-write; running serve-start again would be a no-op at
	# best, so skip it.
	if [[ "${WRITE_BACKEND}" == "${QUERY_BACKEND}" ]]; then
		echo "   write and query backend are both '${QUERY_BACKEND}'; service already started by serve-write"
		return 0
	fi
	_phase_serve_backend "${QUERY_BACKEND}" "${Q_SERVICE}" "serve-query"
}

phase_check() {
	local w target
	for w in ${WRITERS}; do
		target="${DATA_NAME}_${w}"
		say "check: ${target} from ${QUERY_HOST}"
		BE="${QUERY_BACKEND}" remote "${QUERY_HOST}" "check-${target}" check "$target" || FAILED=1
	done
}

phase_query() {
	local w target
	for w in ${WRITERS}; do
		target="${DATA_NAME}_${w}"
		say "query: ${target} from ${QUERY_HOST}"
		BE="${QUERY_BACKEND}" remote "${QUERY_HOST}" "query-${target}" query "$target" || FAILED=1
	done
}

phase_live() {
	say "live: ${LIVE_WRITER} writer at ${LIVE_RATE} records/s per server while ${QUERY_HOST} polls"
	if [[ "$LAUNCHER" == "manual" ]]; then
		on_servers "write-${LIVE_TARGET}" write "${LIVE_WRITER}" "${LIVE_TARGET}" live
		BE="${QUERY_BACKEND}" remote "${QUERY_HOST}" "check-live" check "${LIVE_TARGET}" live
		return 0
	fi
	BE="${WRITE_BACKEND}" on_servers "write-${LIVE_TARGET}" write "${LIVE_WRITER}" "${LIVE_TARGET}" live &
	local writers_pid=$!
	sleep 3
	BE="${QUERY_BACKEND}" remote "${QUERY_HOST}" "check-live" check "${LIVE_TARGET}" live || FAILED=1
	wait "$writers_pid" || FAILED=1
}

phase_stop() {
	# When both backends are the same, their service was started by
	# serve-write (prefix "serve-write"), and serve-query was skipped. Stop
	# with the prefix the service was actually started under so that the
	# Slurm launcher finds the right srun pid file.
	if [[ "${WRITE_BACKEND}" == "${QUERY_BACKEND}" ]]; then
		_phase_stop_backend "${WRITE_BACKEND}" "${W_SERVICE}" "serve-write"
	else
		# Stop the query backend's service first (it may depend on the write
		# backend's data), then the write backend's.
		_phase_stop_backend "${QUERY_BACKEND}" "${Q_SERVICE}" "serve-query"
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

# ------------------------------------------------------------- phase list
# Expanded once; shared by every pair. 'deploy' is pulled out and run once,
# up front, since it copies the whole directory and does not depend on which
# backend pair is running.
PHASES=()
for phase in "$@"; do
	case "$phase" in
	all)         PHASES+=(build serve-write write serve-query check query stop) ;;
	all-nocheck) PHASES+=(build serve-write write serve-query query stop) ;;
	*)           PHASES+=("$phase") ;;
	esac
done

RUN_PHASES=()
DID_DEPLOY=0
for phase in "${PHASES[@]}"; do
	if [[ "$phase" == "deploy" ]]; then
		if [[ $DID_DEPLOY -eq 0 ]]; then
			say "phase: deploy"
			phase_deploy
			DID_DEPLOY=1
		fi
	else
		RUN_PHASES+=("$phase")
	fi
done

# ------------------------------------------------------- per-pair driver
run_pair() { # WRITE_BACKEND QUERY_BACKEND
	WRITE_BACKEND="$1" QUERY_BACKEND="$2"
	PAIR_NAME="${WRITE_BACKEND}"
	[[ "${WRITE_BACKEND}" != "${QUERY_BACKEND}" ]] && PAIR_NAME="${WRITE_BACKEND}-${QUERY_BACKEND}"
	# RUN_DIR and RESULTS_DIR: namespaced per pair, fixed once here for the
	# whole pair's run and reused by every phase below (build, write, check,
	# query, ...) regardless of which single backend -- write or query -- is
	# actually doing the work for a given phase. That is what keeps e.g. a
	# sos:dsos pair's write phase (handled by sos) and check/query phases
	# (handled by dsos, reading the same on-disk containers sos wrote)
	# agreeing on where the data is: both read the one fixed PAIR_NAME-
	# derived RUN_DIR, rather than a path keyed by whichever single backend
	# happens to be running that particular phase.
	RUN_DIR="${BASE_RUN_DIR}/${PAIR_NAME}"
	RESULTS_DIR="${BASE_RESULTS_DIR}/${PAIR_NAME}"
	LOG_DIR="${RESULTS_DIR}/logs"
	mkdir -p "$LOG_DIR"
	FAILED=0

	check_plugin "${WRITE_BACKEND}"
	check_plugin "${QUERY_BACKEND}"

	# The write backend supplies the writers and their default settings.
	# shellcheck source=../backends/sos/sos_backend.conf
	source "${BENCH_DIR}/backends/${WRITE_BACKEND}/${WRITE_BACKEND}_backend.conf"
	WRITERS="${WRITERS:-${DEFAULT_WRITERS:-}}"
	LIVE_WRITER="${LIVE_WRITER:-${WRITERS%% *}}"
	# Does the write backend require a service on the servers?
	W_SERVICE=0
	{
		PLUGIN_SERVICE=0
		source "${BENCH_DIR}/backends/${WRITE_BACKEND}/${WRITE_BACKEND}_backend.conf"
		W_SERVICE="${PLUGIN_SERVICE:-0}"
	} 2>/dev/null

	# Does the query backend require a service, and what data can it read?
	Q_SERVICE=0
	Q_READS=""
	{
		PLUGIN_SERVICE=0 PLUGIN_READS=""
		source "${BENCH_DIR}/backends/${QUERY_BACKEND}/${QUERY_BACKEND}_backend.conf"
		Q_SERVICE="${PLUGIN_SERVICE:-0}"
		Q_READS="${PLUGIN_READS:-}"
	} 2>/dev/null

	# Re-source the write backend so its settings (not the query backend's)
	# are the ones forwarded to the write nodes.
	source "${BENCH_DIR}/backends/${WRITE_BACKEND}/${WRITE_BACKEND}_backend.conf"

	# The query backend must be able to read what the write backend produced.
	if [[ -n "${Q_READS}" && " ${Q_READS} " != *" ${WRITE_BACKEND} "* ]]; then
		echo "backend '${QUERY_BACKEND}' cannot query data written by '${WRITE_BACKEND}' (it reads: ${Q_READS})" >&2
		exit 1
	fi

	[[ -n "${WRITERS}" ]] || { echo "no WRITERS set and backend ${WRITE_BACKEND} has no DEFAULT_WRITERS" >&2; exit 1; }

	# Settings forwarded to every remote step so that environment overrides
	# given to run.sh apply on all nodes, not only here.
	CONFIG_VARS=(BE WRITE_BACKEND QUERY_BACKEND SERVER_HOSTS QUERY_HOST BENCH_DIR RUN_DIR RESULTS_DIR
		ENV_SETUP PYTHON DATASET WRITERS DATA_NAME COMPONENT_IDS_PER_SERVER SAMPLES T0
		INTERVAL JOB_LEN BATCH QUERY_ADAPTER QUERY_FILE REPEAT LIMIT_N
		LIVE_WRITER LIVE_TARGET LIVE_RATE LIVE_BATCH LIVE_SAMPLES LIVE_POLL)
	# Forward each plugin's own settings too, from both backends.
	for _bc in "${WRITE_BACKEND}" "${QUERY_BACKEND}"; do
		_pv="$(PLUGIN_VARS=""; source "${BENCH_DIR}/backends/${_bc}/${_bc}_backend.conf" 2>/dev/null; echo "${PLUGIN_VARS:-}")"
		for _v in ${_pv}; do
			[[ " ${CONFIG_VARS[*]} " == *" ${_v} "* ]] || CONFIG_VARS+=("${_v}")
		done
	done

	echo "monbench: pair=${PAIR_NAME} write=${WRITE_BACKEND} query=${QUERY_BACKEND} launcher=${LAUNCHER} servers=${SERVERS[*]} query-host=${QUERY_HOST}"
	echo "          ${COMPONENT_IDS_PER_SERVER} component IDs x ${SAMPLES} samples per server, writers: ${WRITERS}"
	echo "          data: ${RUN_DIR}"
	echo "          results: ${RESULTS_DIR}"

	for phase in "${RUN_PHASES[@]}"; do
		say "phase: ${phase} (${PAIR_NAME})"
		case "$phase" in
		build) phase_build || FAILED=1 ;;
		serve-write) phase_write_serve ;;
		write) phase_write ;;
		serve-query) phase_query_serve ;;
		check) phase_check ;;
		query) phase_query ;;
		live) phase_live ;;
		stop) phase_stop ;;
		clean) phase_clean ;;
		*) echo "unknown phase '$phase'" >&2; exit 2 ;;
		esac
	done

	say "pair ${PAIR_NAME} done: $([[ $FAILED -eq 0 ]] && echo 'all steps succeeded' || echo 'one or more steps FAILED')"
	[[ $FAILED -eq 0 ]] || OVERALL_FAILED=1
}

for _pair in "${PAIRS[@]}"; do
	run_pair "${_pair%%:*}" "${_pair#*:}"
done

say "done: $([[ $OVERALL_FAILED -eq 0 ]] && echo 'all pairs succeeded' || echo 'one or more pairs FAILED')"
exit $OVERALL_FAILED
