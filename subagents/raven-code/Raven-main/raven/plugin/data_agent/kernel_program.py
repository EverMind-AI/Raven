"""Self-contained data-agent kernel: one process owning the DuckDB plane.

This file is a *program*, not a raven module: the client copies its source
into the workspace and launches it with a configurable Python interpreter
(``kernel_python``), which in an evaluation sandbox is the task image's
interpreter -- the one that has duckdb/pymongo/pandas installed. It must
therefore import nothing from raven and nothing beyond stdlib at module
scope; duckdb loads at bootstrap, pymongo/pandas only if a mongo source is
configured.

Why a subprocess owns the connection instead of the host process:

- TEMP tables (semantic_map materializations) are per-connection, so every
  tool and the model's own Python code must share one connection;
- the model's ``python`` cells get the literal ``con`` variable, which only
  works when the cells run where the connection lives;
- a wedged query can be SIGKILLed without taking the agent down, and the
  restart message can say exactly what was lost.

Protocol: one JSON object per line on stdin/stdout. During ``exec`` the
cell's prints are captured; the protocol channel keeps the real stdout.

Tier A/B instrumentation (design doc section 6.1) rides on every exec:

- Tier A ``[AUTO-INSPECT]``: if the cell ends in an expression, it is
  evaluated exactly once (Jupyter semantics, no re-eval side effects) and,
  for DataFrame/Series-shaped values, shape/dtypes/head are appended.
- Tier B ``[VERIFY]``: merge fan-out, groupby row-growth and empty-result
  checks over the DataFrames the cell (re)bound, PASS lines carrying the
  observed numbers. All checks are best-effort and never break the cell.
"""

from __future__ import annotations

import ast
import io
import json
import re
import sys
import traceback

MAX_REPR = 2_000
HEAD_ROWS = 3


def _jsonable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _is_frame(obj) -> bool:
    return type(obj).__name__ == "DataFrame" and hasattr(obj, "shape") and hasattr(obj, "dtypes")


def _is_series(obj) -> bool:
    return type(obj).__name__ == "Series" and hasattr(obj, "shape")


class Kernel:
    def __init__(self) -> None:
        self.con = None
        self.ns: dict = {}
        self.artifact_dir = ""

    # -- bootstrap ---------------------------------------------------------

    def bootstrap(self, req: dict) -> dict:
        import duckdb

        notes: list[str] = []
        self.con = duckdb.connect()
        self.artifact_dir = req.get("artifact_dir") or ""
        extension_dir = req.get("extension_dir")
        if extension_dir:
            self.con.execute(f"SET extension_directory = '{extension_dir}'")
        scanners: set[str] = set()

        def load_scanner(name: str) -> None:
            # LOAD first: in an offline sandbox INSTALL cannot download, so the
            # extension must already sit in the image; INSTALL is only a
            # convenience fallback for connected dev machines.
            if name in scanners:
                return
            try:
                self.con.execute(f"LOAD {name}")
            except Exception:
                self.con.execute(f"INSTALL {name}; LOAD {name};")
            scanners.add(name)

        for src in req.get("sources", []):
            alias, kind, ref = src["alias"], src["kind"], src["ref"]
            read_only = src.get("read_only", True)
            try:
                if kind == "sqlite":
                    load_scanner("sqlite_scanner")
                    ro = ", READ_ONLY" if read_only else ""
                    self.con.execute(f"ATTACH '{ref}' AS {alias} (TYPE sqlite{ro})")
                elif kind == "duckdb":
                    ro = " (READ_ONLY)" if read_only else ""
                    self.con.execute(f"ATTACH '{ref}' AS {alias}{ro}")
                elif kind == "postgres":
                    load_scanner("postgres_scanner")
                    ro = ", READ_ONLY" if read_only else ""
                    self.con.execute(f"ATTACH '{ref}' AS {alias} (TYPE postgres{ro})")
                elif kind == "mongo":
                    notes.extend(self._materialize_mongo(alias, ref))
                else:
                    notes.append(f"[ATTACH SKIPPED] {alias}: unsupported kind {kind!r}")
                if kind in ("sqlite", "duckdb", "postgres"):
                    notes.append(f"attached {alias} ({kind})")
            except Exception as exc:
                notes.append(f"[ATTACH FAILED] {alias} ({kind}): {exc}")
        self.ns = {"con": self.con}
        return {"ok": True, "notes": notes}

    def _materialize_mongo(self, alias: str, uri: str) -> list[str]:
        """Copy every collection of a mongo db into ``<alias>.<collection>`` tables.

        Mongo has no DuckDB scanner, so the session pays a one-time
        materialization instead of a per-query bridge; afterwards mongo data
        is ordinary SQL like every other source.
        """
        import pandas as pd
        from pymongo import MongoClient

        notes: list[str] = []
        client = MongoClient(uri)
        db = client.get_default_database()
        self.con.execute(f'CREATE SCHEMA IF NOT EXISTS "{alias}"')
        for coll_name in db.list_collection_names():
            docs = list(db[coll_name].find())
            for doc in docs:
                if "_id" in doc:
                    doc["_id"] = str(doc["_id"])
            frame = pd.DataFrame(docs)
            self.con.register("_mongo_incoming", frame)
            self.con.execute(f'CREATE TABLE "{alias}"."{coll_name}" AS SELECT * FROM _mongo_incoming')
            self.con.unregister("_mongo_incoming")
            notes.append(f"materialized mongo collection {alias}.{coll_name}: {len(docs)} rows")
        client.close()
        return notes

    # -- sql ---------------------------------------------------------------

    def sql(self, req: dict) -> dict:
        query = req["query"]
        preview = int(req.get("preview", 50))
        try:
            rel = self.con.sql(query)
            cols = list(rel.columns)
            rows = rel.fetchall()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        saved = None
        save_as = req.get("save_as")
        if save_as:
            raw = req.get("raw_query") or query
            saved = f"{self.artifact_dir}/{save_as}.parquet"
            try:
                self.con.execute(f"COPY ({raw.strip().rstrip(';')}) TO '{saved}' (FORMAT PARQUET)")
            except Exception as exc:
                return {"ok": False, "error": f"result computed but saving to {saved} failed: {exc}"}
        return {
            "ok": True,
            "cols": cols,
            "rows": [[_jsonable(v) for v in row] for row in rows[:preview]],
            "row_count": len(rows),
            "saved": saved,
        }

    # -- put_rows ------------------------------------------------------------

    def put_rows(self, req: dict) -> dict:
        """Materialize tool-computed rows (e.g. semantic_map output) as a table."""
        name = req["name"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            return {"ok": False, "error": f"invalid table name {name!r}; use [A-Za-z_][A-Za-z0-9_]*"}
        cols = req["cols"]
        rows = req["rows"]
        try:
            col_defs = ", ".join(f'"{c}" VARCHAR' for c in cols)
            self.con.execute(f'CREATE OR REPLACE TABLE "{name}" ({col_defs})')
            if rows:
                placeholders = ", ".join("?" for _ in cols)
                self.con.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', rows)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "row_count": len(rows)}

    # -- exec with Tier A/B ------------------------------------------------

    def exec_cell(self, req: dict) -> dict:
        code = req["code"]
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError:
            return {"ok": False, "error": traceback.format_exc(limit=0).strip()}
        trailing = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            trailing = ast.Expression(tree.body.pop().value)
        bound_names = _assigned_names(tree)
        pre_lens = _frame_lens(self.ns)

        captured = io.StringIO()
        real_stdout, real_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = captured
        value = _NOTHING
        try:
            exec(compile(tree, "<cell>", "exec"), self.ns)
            if trailing is not None:
                value = eval(compile(trailing, "<cell>", "eval"), self.ns)
        except Exception:
            return {
                "ok": False,
                "stdout": captured.getvalue()[-MAX_REPR:],
                "error": traceback.format_exc(limit=8),
            }
        finally:
            sys.stdout, sys.stderr = real_stdout, real_stderr

        inspect = None if value is _NOTHING else _auto_inspect(value)
        verify = _verify_checks(code, bound_names, self.ns, pre_lens)
        return {
            "ok": True,
            "stdout": captured.getvalue()[-8_000:],
            "inspect": inspect,
            "verify": verify,
        }


_NOTHING = object()


def _assigned_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                names.append(target.id)
    return names


def _frame_lens(ns: dict) -> dict:
    lens = {}
    for name, obj in list(ns.items()):
        try:
            if _is_frame(obj):
                lens[name] = len(obj)
        except Exception:
            continue
    return lens


def _auto_inspect(value) -> str:
    """Describe the cell's trailing expression from its already-computed value.

    The value comes from the single ``eval`` in exec_cell -- never re-evaluate
    the expression here, or side-effecting cells run twice.
    """
    try:
        if _is_frame(value):
            dtypes = ", ".join(f"{k}:{v}" for k, v in list(value.dtypes.astype(str).items())[:20])
            head = value.head(HEAD_ROWS).to_string()
            return f"[AUTO-INSPECT] DataFrame shape={value.shape} dtypes=({dtypes})\n{head}"
        if _is_series(value):
            return f"[AUTO-INSPECT] Series len={value.shape[0]} dtype={value.dtype}\n{value.head(HEAD_ROWS).to_string()}"
        text = repr(value)
        return text if len(text) <= MAX_REPR else text[:MAX_REPR] + f"... (repr truncated at {MAX_REPR} chars)"
    except Exception as exc:
        return f"[AUTO-INSPECT failed: {exc}]"


def _verify_checks(code: str, bound_names: list[str], ns: dict, pre_lens: dict) -> list[str]:
    """Post-hoc sanity assertions on the DataFrames this cell (re)bound."""
    out: list[str] = []
    frames = {}
    for name in bound_names:
        obj = ns.get(name)
        try:
            if _is_frame(obj):
                frames[name] = len(obj)
        except Exception:
            continue
    try:
        for m in re.finditer(r"(\w+)\s*=\s*(\w+)\s*\.\s*merge\(\s*(\w+)", code):
            result, left, right = m.group(1), m.group(2), m.group(3)
            sizes = [pre_lens.get(left), pre_lens.get(right)]
            if result in frames and all(s is not None for s in sizes):
                biggest = max(sizes)
                if frames[result] > 2 * biggest:
                    out.append(
                        f"[VERIFY: FAIL] merge: {result} has {frames[result]} rows > 2x largest input "
                        f"({left}={sizes[0]}, {right}={sizes[1]}) -- join key is probably not unique; "
                        "check fan-out before aggregating."
                    )
                else:
                    out.append(
                        f"[VERIFY: PASS] merge: {result}={frames[result]} rows from {left}={sizes[0]}, {right}={sizes[1]}."
                    )
        for m in re.finditer(r"(\w+)\s*=\s*(\w+)\s*\.\s*groupby\(", code):
            result, source = m.group(1), m.group(2)
            if result in frames and source in pre_lens:
                if frames[result] > pre_lens[source]:
                    out.append(
                        f"[VERIFY: FAIL] groupby: {result} has {frames[result]} rows, more than its source "
                        f"{source}={pre_lens[source]} -- a group-then-aggregate must not add rows."
                    )
                else:
                    out.append(
                        f"[VERIFY: PASS] groupby: {result}={frames[result]} groups from {source}={pre_lens[source]} rows."
                    )
        for name, size in frames.items():
            if size == 0:
                out.append(
                    f"[VERIFY: WARN] {name} is empty (0 rows) after this cell. An empty frame is a valid state "
                    "but is usually a wrong filter -- diagnose before building on it."
                )
    except Exception:
        pass
    return out


def main() -> None:
    proto_out = sys.stdout
    kernel = Kernel()
    handlers = {
        "bootstrap": kernel.bootstrap,
        "sql": kernel.sql,
        "exec": kernel.exec_cell,
        "put_rows": kernel.put_rows,
        "ping": lambda req: {"ok": True},
    }
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req: dict = {}
        try:
            req = json.loads(line)
            resp = handlers[req["op"]](req)
        except Exception:
            resp = {"ok": False, "error": traceback.format_exc(limit=4)}
        resp["id"] = req.get("id")
        proto_out.write(json.dumps(resp, default=str) + "\n")
        proto_out.flush()


if __name__ == "__main__":
    main()
