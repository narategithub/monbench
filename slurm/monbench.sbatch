#!/usr/bin/env bash
# Run the two-server D/SOS trial as one batch job on three nodes.
# The first two nodes of the allocation become servers, the third the query
# node (unless SERVER_NODES / QUERY_NODE are exported before sbatch).
#
#   sbatch slurm/monbench.sbatch                 # build serve-write write serve check stop
#   sbatch slurm/monbench.sbatch build serve-write write serve check live stop
#
#SBATCH --job-name=monbench
#SBATCH --nodes=3
#SBATCH --ntasks-per-node=1
#SBATCH --time=00:30:00
#SBATCH --output=monbench-%j.out

set -euo pipefail
# sbatch runs a copy of this script, so locate the tree from the submit dir.
cd "${SLURM_SUBMIT_DIR}"
PHASES=("$@")
[[ ${#PHASES[@]} -gt 0 ]] || PHASES=(all)
bin/run.sh -l slurm "${PHASES[@]}"
