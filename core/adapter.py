"""adapter.py -- the interface every backend's query adapter implements.

A backend plugin provides backends/<name>/query.py, which defines one or more
subclasses of Adapter and two module attributes:

    ADAPTERS = {"<adapter-name>": AdapterClass, ...}
    DEFAULT_ADAPTER = "<adapter-name>"

The engine (core/check.py) loads that module by file path, so plugins need no
installation and no change to the core.
"""


class BackendError(Exception):
    """The backend cannot be used: not reachable, wrong type, or no data."""


class Adapter:
    #: Name used in QUERY_ADAPTER and as a key in query-set files.
    name = None
    #: Query language. Query sets may give one string per dialect, so
    #: adapters that share a language share query strings.
    dialect = None

    def __init__(self, ctx):
        """ctx: dict with
             target   name of the data written (container, database, ...)
             schema   data-set name (the record type)
             servers  list of server hosts from SERVER_NODES
             run_dir  scratch directory on this node
             env      the harness settings (os.environ)
        """
        self.ctx = ctx

    def connect(self):
        """Connect and confirm the backend holds the target's data.

        Raise BackendError if it does not: that stops the run. Not timed.
        """
        raise NotImplementedError

    def query(self, text):
        """Run one query; yield its result in batches.

        The engine times from the call until the last batch has been
        consumed, so do only what a normal client of this backend would do.
        """
        raise NotImplementedError

    @staticmethod
    def count(batch):
        """Number of rows in a batch. Called inside the timed region."""
        return len(batch)

    def records(self, batch):
        """Return (columns, rows) for a batch; rows are dicts keyed by
        attribute name. Used for verification only; not timed."""
        raise NotImplementedError

    def close(self):
        pass
