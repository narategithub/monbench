"""Template query adapter. Copy to backends/<name>/query.py and fill in.

See core/adapter.py for the interface and backends/README.md for the rules.
"""
from adapter import Adapter, BackendError


class TemplateAdapter(Adapter):
    name = "template"          # QUERY_ADAPTER value, and query-set key
    dialect = "template"       # query language; query sets may key on it

    def connect(self):
        # Connect using settings from self.ctx["env"] (backend.conf), then
        # confirm that self.ctx["target"] exists and holds self.ctx["schema"]
        # records. Raise BackendError otherwise.
        raise BackendError("template backend: connect() not implemented")

    def query(self, text):
        # Run the query and yield its result in batches, as the client
        # library delivers them.
        raise NotImplementedError

    def records(self, batch):
        # Return (columns, rows), rows being dicts keyed by attribute name.
        raise NotImplementedError


ADAPTERS = {TemplateAdapter.name: TemplateAdapter}
DEFAULT_ADAPTER = TemplateAdapter.name
