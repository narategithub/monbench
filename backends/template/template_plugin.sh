# <name>_plugin.sh -- backend hooks, sourced by bin/node.sh. Copy this
# directory to backends/<name>/ and rename each file with your backend's
# name as a prefix: <name>_plugin.sh, <name>_backend.conf,
# query/<name>_query.py. See backends/README.md for what each hook must do.
# Hooks you do not define are skipped; plugin_write is required.
#
# Available from node.sh: HOST, PLUGIN_DIR, log, die, all_targets,
# dataset_args (array, in plugin_write), and every setting from bench.conf
# and <name>_backend.conf.

# plugin_build() {
# 	: # compile writers, if any
# }

plugin_write() { # WRITER TARGET MODE INDEX SERVER_ID
	local writer="$1" target="$2" index="$4"
	# Replace any existing data for this partition of TARGET, then write
	# partition INDEX of the data set. Print one JSON summary line; see
	# backends/README.md.
	die "template backend: plugin_write not implemented (writer ${writer}, target ${target}, partition ${index})"
}

# plugin_serve_start() { :; }   # SERVER_ID; start the service in the background
# plugin_serve_run() { :; }     # SERVER_ID; run it in the foreground (Slurm)
# plugin_serve_stop() { :; }
# plugin_serve_status() { :; }  # return 0 when the service is ready
# plugin_clean() { :; }         # SERVER_ID; delete this server's data
