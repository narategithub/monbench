"""SOS direct query adapter for the benchmark engine.

Queries SOS containers directly on disk, without going through dsosd.
This provides a single-node (or shared-filesystem) comparison point:
the same SqlQuery interface as the dsos adapter, but with no RPC layer.

The adapter opens each server's container from the query node, so the
containers must be accessible from the query node — either on a shared
filesystem, or the query node is also a server node.

The container path for each server is constructed as:
    SOS_DATA_ROOT/<server>/<target>

This must match the path the writer used. When SOS_DATA_ROOT includes
${HOSTNAME} (as it does by default on the write side), the query-side
config must override it to a path that resolves correctly from the query
node — typically the shared-filesystem prefix without ${HOSTNAME}.

Settings (from sos_backend.conf / environment):
    SOS_DATA_ROOT   root of the containers
    SOS_ROW_LIMIT   rows per batch (default 4096)
"""
import os

from adapter import Adapter, BackendError


def _probe(ctx):
    return f"select timestamp from {ctx['schema']} order_by timestamp limit 1"


class SosAdapter(Adapter):
    """Query SOS containers directly on disk (no dsosd)."""

    name = "sos"
    dialect = "dsos"      # same query language as dsos

    def connect(self):
        try:
            from sosdb import Sos
        except ImportError as e:
            raise BackendError(f"the sosdb Python module is not available: {e}")
        self._Sos = Sos

        ctx = self.ctx
        env = ctx["env"]
        self.row_limit = int(env.get("SOS_ROW_LIMIT", "4096"))
        data_root = env.get("SOS_DATA_ROOT", "")
        if not data_root:
            raise BackendError("SOS_DATA_ROOT is not set")

        # Open each server's container directly.
        self._containers = []
        for server in ctx["servers"]:
            path = os.path.join(data_root, server, ctx["target"])
            if not os.path.exists(path):
                raise BackendError(
                    f"container not found: {path}  "
                    f"(SOS_DATA_ROOT may need to be set to a path "
                    f"accessible from the query node)")
            try:
                cont = Sos.Container()
                cont.open(path, o_perm=Sos.PERM_RD)
            except Exception as e:
                raise BackendError(f"cannot open container '{path}': {e}")
            self._containers.append(cont)

        # Probe the first container to verify the schema exists.
        try:
            rows = sum(self.count(b) for b in self._query_one(
                self._containers[0], _probe(ctx)))
        except Exception as e:
            raise BackendError(
                f"schema '{ctx['schema']}' cannot be queried: {e}")
        if rows == 0:
            raise BackendError(
                f"container for server '{ctx['servers'][0]}' holds no "
                f"'{ctx['schema']}' records")

    def _query_one(self, cont, text):
        """Run a query against a single container, yielding DataFrames."""
        q = cont.query(self.row_limit)
        q.select(text)
        for df in q:
            yield df

    def query(self, text):
        """Run the query against every server's container and yield batches."""
        for cont in self._containers:
            yield from self._query_one(cont, text)

    def records(self, df):
        return list(df.columns), df.to_dict("records")

    def close(self):
        for cont in self._containers:
            try:
                cont.close()
            except Exception:
                pass
        self._containers = []


ADAPTERS = {SosAdapter.name: SosAdapter}
DEFAULT_ADAPTER = "sos"
