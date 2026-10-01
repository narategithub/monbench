# influxdb_plugin.sh -- InfluxDB v3 Core hooks, sourced by bin/node.sh when BACKEND=influxdb.
#
# Hooks (see backends/README.md for the contract):
#   plugin_write       WRITER TARGET MODE INDEX ID  write one partition
#   plugin_serve_start ID                           generate a token (offline),
#                                                   start influxdb3, create the
#                                                   database
#   plugin_serve_run   ID                           foreground influxdb3
#                                                   (slurm launcher)
#   plugin_serve_stop                               stop influxdb3
#   plugin_clean       ID                           remove data directory and
#                                                   generated token file
#
# No build step (the writer is Python).
# Available from node.sh: HOST, PLUGIN_DIR, log, die, dataset_args (array),
# and every setting from bench.conf and influxdb_backend.conf.

INFLUX_DIR="${BENCH_DIR}/backends/influxdb"

# All runtime files live under RUN_DIR so the harness owns the full lifecycle
# and clean wipes everything in one place.
_INFLUX_PID="${RUN_DIR}/influxdb3.pid"
_INFLUX_LOG="${RUN_DIR}/influxdb3.log"

# ----------------------------------------------------------------- write

plugin_write() { # WRITER TARGET MODE INDEX SERVER_ID
	log "token file: ${INFLUX_TOKEN_FILE}"
	local writer="$1" index="$4"
	# $2 (target) is part of the plugin_write contract but unused here: the
	# measurement name is DATA_NAME, not the per-writer target string, so
	# every backend's query strings can address it with {schema} uniformly.
	[[ "$writer" == "influx" ]] ||
		die "the influxdb backend has no writer '$writer' (available: influx)"

	# dataset_args carries a generic --batch sized for SOS transactions.
	# InfluxDB's write batch is INFLUX_WRITE_BATCH, so drop --batch and its
	# value and pass the InfluxDB one instead. --rate is kept for the live test.
	local args=() skip=0 a
	for a in "${dataset_args[@]}"; do
		if [[ "$skip" == 1 ]]; then skip=0; continue; fi
		if [[ "$a" == "--batch" ]]; then skip=1; continue; fi
		args+=("$a")
	done

	local token
	token="$(_influx_read_token)"

	log "writing ${DATA_NAME} to ${INFLUX_DATABASE} (influx writer, ${INFLUX_INTERFACE} interface) on ${INFLUX_HOST}"
	"${PYTHON}" "${INFLUX_DIR}/writer/influx_write.py" \
		--measurement "$DATA_NAME" --node-index "$index" \
		"${args[@]}" --batch "${INFLUX_WRITE_BATCH}" \
		--interface "${INFLUX_INTERFACE}" \
		--host "${INFLUX_HOST}" \
		--database "${INFLUX_DATABASE}" \
		--precision "${INFLUX_PRECISION}" \
		--token "$token"
}

# ----------------------------------------------------------------- helpers

# Extract the token string from the JSON token file written by serve-start.
_influx_read_token() {
	[[ -f "${INFLUX_TOKEN_FILE}" ]] \
		|| die "token file not found: ${INFLUX_TOKEN_FILE}; was serve-write run?"
	"${PYTHON}" -c "import json,sys; print(json.load(open(sys.argv[1]))['token'])" \
		"${INFLUX_TOKEN_FILE}"
}

# ----------------------------------------------------------------- serve

plugin_serve_start() { # SERVER_ID
	mkdir -p "${INFLUX_DATA_DIR}" "${RUN_DIR}"

        # Kill any leftover process from a previous run before starting a new one.
        pkill -f 'influxdb3 serve' 2>/dev/null || true
        sleep 1

	# Generate an admin token offline so we have it before the server starts.
	# --offline writes a JSON file; the server is then started with that file
	# so auth is active from the first request. The CLI also prints the
	# token to its own stdout (even with --output-file given); redirect that
	# to the influxdb3 log instead of letting it reach the terminal, and
	# fall back to the token file if a reader needs it for debugging.
	log "generating admin token"
	influxdb3 create token --admin \
		--offline \
		--name "monbench-admin" \
		--output-file "${INFLUX_TOKEN_FILE}" \
		>>"${_INFLUX_LOG}" 2>&1 \
		|| die "token generation failed; see ${_INFLUX_LOG}" \
		>/dev/null

	log "starting influxdb3 (data: ${INFLUX_DATA_DIR})"
	influxdb3 serve \
		--node-id "${HOST}" \
		--object-store file \
		--data-dir "${INFLUX_DATA_DIR}" \
		--admin-token-file "${INFLUX_TOKEN_FILE}" \
		>>"${_INFLUX_LOG}" 2>&1 &
	echo $! >"${_INFLUX_PID}"

	# Give the process a moment to bind its port; confirm it is still alive.
	sleep 2
	kill -0 "$(cat "${_INFLUX_PID}")" 2>/dev/null \
		|| die "influxdb3 exited immediately; see ${_INFLUX_LOG}"
	log "influxdb3 started"

	# Create the database using the generated token.
	local token
	token="$(_influx_read_token)"
	influxdb3 create database "${INFLUX_DATABASE}" \
		--host "${INFLUX_HOST}" \
		--token "$token" 2>/dev/null || true
	log "database '${INFLUX_DATABASE}' ready"
}

plugin_serve_run() { # SERVER_ID  (foreground; used by the slurm launcher)
	# The token file must already exist (serve_start ran first in the slurm
	# flow before serve_run is called for background operation).
	mkdir -p "${INFLUX_DATA_DIR}" "${RUN_DIR}"
	exec influxdb3 serve \
		--node-id "${HOST}" \
		--object-store file \
		--data-dir "${INFLUX_DATA_DIR}" \
		--admin-token-file "${INFLUX_TOKEN_FILE}"
}

plugin_serve_stop() {
	if [[ -f "${_INFLUX_PID}" ]]; then
		local pid
		pid="$(cat "${_INFLUX_PID}")"
		kill "${pid}" 2>/dev/null || true
		# Wait briefly so the data directory is not held open during clean.
		local i
		for i in $(seq 1 10); do
			kill -0 "${pid}" 2>/dev/null || break
			sleep 1
		done
		rm -f "${_INFLUX_PID}"
	fi
	log "influxdb3 stopped"
}

# ----------------------------------------------------------------- clean

plugin_clean() { # SERVER_ID
	plugin_serve_stop
	if [[ -d "${INFLUX_DATA_DIR}" ]]; then
		rm -rf "${INFLUX_DATA_DIR}"
		log "removed ${INFLUX_DATA_DIR}"
	fi
	rm -f "${INFLUX_TOKEN_FILE}"
	log "removed token at ${INFLUX_TOKEN_FILE}"
}
