"""SOS/DSOS query adapters for the benchmark engine.

  dsos    DSOS through the Python API (sosdb.Sos), returning DataFrames.
          Use this one for timings.
  dsosql  DSOS through the dsosql command, parsing its CSV output. Each query
          starts a dsosql process that connects to every server, so timings
          include that start-up; useful as a cross-check only.

Settings (backends/sos/backend.conf): SOS_ROW_LIMIT, DSOSQL.
"""
import os
import re
import subprocess

from adapter import Adapter, BackendError


def _session_file(ctx):
    """DSOS session file: the server hosts, one per line."""
    os.makedirs(ctx["run_dir"], exist_ok=True)
    path = os.path.join(ctx["run_dir"], "dsos-session.txt")
    with open(path, "w") as f:
        f.write("\n".join(ctx["servers"]) + "\n")
    return path


def _probe(ctx):
    return f"select timestamp from {ctx['schema']} order_by timestamp limit 1"


class DsosAdapter(Adapter):
    name = "dsos"
    dialect = "dsos"

    def connect(self):
        try:
            from sosdb import Sos
        except ImportError as e:
            raise BackendError(f"the sosdb Python module is not available: {e}")
        ctx = self.ctx
        self.row_limit = int(ctx["env"].get("SOS_ROW_LIMIT", "4096"))
        try:
            self.session = Sos.Session(_session_file(ctx))
        except Exception as e:
            raise BackendError(f"cannot attach to DSOS servers "
                               f"{', '.join(ctx['servers'])}: {e}")
        try:
            self.cont = self.session.open(ctx["target"], o_perm=Sos.PERM_RD)
        except Exception as e:
            raise BackendError(f"cannot open container '{ctx['target']}': {e}")
        try:
            rows = sum(self.count(b) for b in self.query(_probe(ctx)))
        except Exception as e:
            raise BackendError(f"schema '{ctx['schema']}' cannot be queried: {e}")
        if rows == 0:
            raise BackendError(f"container '{ctx['target']}' holds no "
                               f"'{ctx['schema']}' records")

    def query(self, text):
        q = self.cont.query(self.row_limit)
        q.select(text)
        for df in q:
            yield df

    def records(self, df):
        return list(df.columns), df.to_dict("records")

    def close(self):
        self.cont = None
        self.session = None


class DsosqlAdapter(Adapter):
    name = "dsosql"
    dialect = "dsos"
    PROMPT = "dsosql: "

    def connect(self):
        self.session = _session_file(self.ctx)
        self.dsosql = self.ctx["env"].get("DSOSQL", "dsosql")
        batches = list(self.query(_probe(self.ctx)))
        if not batches or not batches[0]:
            raise BackendError(f"no '{self.ctx['schema']}' records reachable in "
                               f"container '{self.ctx['target']}'")

    def query(self, text):
        script = (f"set format csv\n"
                  f"open path {self.ctx['target']} perm ro\n"
                  f"{text}\n")
        env = dict(os.environ, TERM="dumb", INPUTRC="/dev/null")
        proc = subprocess.run([self.dsosql, "-a", self.session], input=script,
                              text=True, capture_output=True, env=env)
        columns, rows, footer, in_csv = [], [], None, False
        errors = [l.strip() for l in proc.stderr.splitlines() if l.strip()]
        for line in proc.stdout.splitlines():
            while line.startswith(self.PROMPT):
                line = line[len(self.PROMPT):]
            if not in_csv:
                if line.startswith("# ") and not line.startswith("# Records"):
                    columns = [c.strip() for c in line[2:].split(",")]
                    in_csv = True
                elif line.startswith("Error"):
                    errors.append(line.strip())
                continue
            m = re.match(r"# Records (\d+)", line)
            if m:
                footer, in_csv = int(m.group(1)), False
            elif line.strip():
                rows.append(dict(zip(columns, line.split(","))))
        if errors or proc.returncode != 0 or footer is None:
            raise BackendError("; ".join(errors) or
                               f"dsosql exited with status {proc.returncode}, no result")
        if footer != len(rows):
            raise BackendError(f"dsosql reported {footer} records, parsed {len(rows)}")
        self._columns = columns
        yield rows

    def records(self, rows):
        return self._columns, rows


ADAPTERS = {a.name: a for a in (DsosAdapter, DsosqlAdapter)}
DEFAULT_ADAPTER = "dsos"
