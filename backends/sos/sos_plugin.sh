# sos_plugin.sh -- SOS storage hooks, sourced by bin/node.sh when BACKEND is sos
# or dsos. The dsos plugin sources this file for its write side.
#
# This backend writes one local SOS container per server, using the SOS API.
# It also provides a direct, on-disk query adapter (query/sos_query.py,
# QUERY_ADAPTER=sos) for a single-node/shared-filesystem comparison point
# with no RPC; see backends/dsos for querying across servers through dsosd.
#
# Hooks (see backends/README.md for the contract):
#   plugin_build                              build the C writer
#   plugin_write WRITER TARGET MODE INDEX ID  write one partition into a local
#                                             SOS container named TARGET
#   plugin_clean ID                           delete this server's containers
#
# Available from node.sh: HOST, PLUGIN_DIR, log, die, all_targets,
# dataset_args (array), and every setting from bench.conf and sos_backend.conf.

# This file's own directory, which is not PLUGIN_DIR when dsos sources it.
SOS_DIR="${BENCH_DIR}/backends/sos"

container_path() { # SERVER_ID TARGET
	echo "${SOS_DATA_ROOT}/$1/$2"
}

# ----------------------------------------------------------------- build

plugin_build() {
	make -C "${SOS_DIR}/writer" SOS_PREFIX="${SOS_PREFIX}"
}

# ----------------------------------------------------------------- write

plugin_write() { # WRITER TARGET MODE INDEX SERVER_ID
	local writer="$1" target="$2" index="$4" id="$5"
	local path template
	path="$(container_path "$id" "$target")"
	# Start from an empty container so that reruns do not add duplicates.
	rm -rf "$path"
	mkdir -p "$(dirname "$path")" "${RUN_DIR}"
	template="${RUN_DIR}/sos-schema-${HOST}.json"
	"${PYTHON}" "${SOS_DIR}/make_schema.py" "${DATASET}" "$template"
	local args=(--path "$path" --template "$template" --node-index "$index"
		"${dataset_args[@]}" --storage "${SOS_STORAGE}")
	log "writing ${target} (${writer} writer, SOS ${SOS_STORAGE}) to ${path}"
	case "$writer" in
#	c)
#		[[ -x "${SOS_DIR}/writer/sos_write" ]] ||
#			die "C writer not built; run: bin/run.sh build"
#		"${SOS_DIR}/writer/sos_write" "${args[@]}"
#		;;
	c)
		[[ -x "${SOS_DIR}/writer/sos_write" ]] ||
			die "C writer not built; run: bin/run.sh build"
		if [[ -n "${GDB:-}" ]]; then
			command -v gdbserver >/dev/null 2>&1 ||
				die "GDB=1 requested but gdbserver is not on PATH"
			local port="${GDBSERVER_PORT:-$((2345 + index))}"
			log "GDB=1: launching sos_write under gdbserver on ${HOST}:${port} -- attach with:" \
				"gdb -ex 'target remote ${HOST}:${port}' ${SOS_DIR}/writer/sos_write"
			gdbserver ":${port}" "${SOS_DIR}/writer/sos_write" "${args[@]}"
		else
			"${SOS_DIR}/writer/sos_write" "${args[@]}"
		fi
		;;
	py)
		"${PYTHON}" "${SOS_DIR}/writer/sos_write.py" "${args[@]}"
		;;
	*)
		die "the sos backend has no writer '$writer' (available: c py)"
		;;
	esac
}

# ----------------------------------------------------------------- clean

# Delete this server's containers. Kept separate so that dsos can reuse it
# after removing its own service files.
sos_clean_data() { # SERVER_ID
	rm -rf "${SOS_DATA_ROOT:?}/$1"
	rm -f "${RUN_DIR}/sos-schema-${HOST}.json"
	log "removed ${SOS_DATA_ROOT}/$1 and this node's schema file"
}

plugin_clean() { # SERVER_ID
	sos_clean_data "$1"
}
