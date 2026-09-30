# dsos_plugin.sh -- DSOS query backend hooks, sourced by bin/node.sh.
#
# DSOS runs one dsosd per server; a query fans out to all of them and the
# client merges the results. This plugin provides the service and clean hooks;
# it has no writer of its own (data is written by the sos backend), and its
# query adapters are in query/dsos_query.py.
#
# Hooks (see backends/README.md):
#   plugin_serve_start ID / plugin_serve_run ID / plugin_serve_stop /
#   plugin_serve_status                       manage dsosd on this server
#   plugin_clean ID                           delete this server's containers
#
# Available from node.sh: HOST, PLUGIN_DIR, log, die, all_targets, and every
# setting from bench.conf and dsos_backend.conf.

# RPC program number of the D/SOS service (program SOSDB in sosapi.x).
SOSDB_PROG=40009862
SOSDB_VERS=1
PIDFILE="${RUN_DIR}/dsosd-${HOST}.pid"
LOGFILE="${RUN_DIR}/dsosd-${HOST}.log"
DIRFILE="${RUN_DIR}/dsosd-directory-${HOST}.json"

container_path() { # SERVER_ID TARGET
	echo "${SOS_DATA_ROOT}/$1/$2"
}

write_directory() { # SERVER_ID
	local id="$1" first=1 name
	mkdir -p "${RUN_DIR}"
	{
		printf '{\n  "%s" : {\n' "$id"
		for name in $(all_targets); do
			[[ $first -eq 1 ]] || printf ',\n'
			printf '    "%s" : "%s"' "$name" "$(container_path "$id" "$name")"
			first=0
		done
		printf '\n  }\n}\n'
	} >"${DIRFILE}"
}

dsosd_registered() {
	rpcinfo -p localhost 2>/dev/null | awk '{print $1}' | grep -qx "${SOSDB_PROG}"
}

# dsosd does not remove its rpcbind registration when it is killed, so the
# harness removes it; otherwise the next start finds a stale registration.
deregister() {
	dsosd_registered || return 0
	rpcinfo -d "${SOSDB_PROG}" "${SOSDB_VERS}" 2>/dev/null ||
		sudo -n rpcinfo -d "${SOSDB_PROG}" "${SOSDB_VERS}" 2>/dev/null || true
	if dsosd_registered; then
		log "could not remove the rpcbind registration; run: sudo rpcinfo -d ${SOSDB_PROG} ${SOSDB_VERS}"
		return 1
	fi
	log "removed the rpcbind registration of program ${SOSDB_PROG}"
}

plugin_serve_start() { # SERVER_ID
	local id="$1"
	command -v rpcinfo >/dev/null || die "rpcinfo not found"
	if dsosd_registered; then
		local ours="" others
		[[ -f "${PIDFILE}" ]] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null && ours="$(cat "${PIDFILE}")"
		if [[ -n "$ours" ]]; then
			log "dsosd started by this harness is already running (pid ${ours})"
			return 0
		fi
		others="$(pgrep -x dsosd | tr '\n' ' ' || true)"
		if [[ -n "$others" ]]; then
			die "a dsosd not started by this harness is running (pid ${others% }). It does not use this harness's directory file, so the checks cannot find the containers. Stop it, then rerun the serve phase."
		fi
		log "removing a stale rpcbind registration (no dsosd process is running)"
		deregister || die "could not remove the stale registration"
	fi
	write_directory "$id"
	log "starting dsosd (server id ${id}, log ${LOGFILE})"
	DSOSD_DIRECTORY="${DIRFILE}" DSOSD_SERVER_ID="$id" \
		nohup ${DSOSD} >"${LOGFILE}" 2>&1 </dev/null &
	echo $! >"${PIDFILE}"
	local i
	for i in $(seq 1 30); do
		dsosd_registered && { log "dsosd is registered"; return 0; }
		sleep 1
	done
	die "dsosd did not register within 30 s; see ${LOGFILE}"
}

plugin_serve_run() { # SERVER_ID -- foreground, used by the Slurm launcher
	local id="$1"
	write_directory "$id"
	echo $$ >"${PIDFILE}"
	log "running dsosd in the foreground (server id ${id})"
	export DSOSD_DIRECTORY="${DIRFILE}" DSOSD_SERVER_ID="$id"
	exec ${DSOSD}
}

plugin_serve_stop() {
	if [[ ! -f "${PIDFILE}" ]]; then
		log "no dsosd pid file; nothing to stop"
		return 0
	fi
	local pid i
	pid="$(cat "${PIDFILE}")"
	rm -f "${PIDFILE}"
	if ! kill "$pid" 2>/dev/null; then
		log "dsosd (pid ${pid}) was not running"
	else
		for i in $(seq 1 20); do
			kill -0 "$pid" 2>/dev/null || break
			sleep 0.5
		done
		if kill -0 "$pid" 2>/dev/null; then
			kill -KILL "$pid" 2>/dev/null || true
		fi
		log "stopped dsosd (pid ${pid})"
	fi
	if ! pgrep -x dsosd >/dev/null; then
		deregister || true
	fi
}

plugin_serve_status() {
	if dsosd_registered; then
		log "dsosd registered (program ${SOSDB_PROG})"
	else
		log "dsosd NOT registered"
		return 1
	fi
}

plugin_clean() { # SERVER_ID
	# Remove dsosd's own run files; the containers belong to the write backend
	# and are removed by its clean phase.
	rm -f "${PIDFILE}" "${DIRFILE}" "${LOGFILE}"
	log "removed dsosd run files on this node"
}
