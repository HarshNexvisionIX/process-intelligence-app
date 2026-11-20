"""Convert an XES event log (.xes or .xes.gz) to Parquet for the process mining app.

Usage:
    python scripts/xes_to_parquet.py <input.xes[.gz]> [output.parquet]

One row per event. Event attributes keep their key with ':' replaced by '_'
(concept:name -> concept_name, time:timestamp -> time_timestamp, org:resource ->
org_resource, lifecycle:transition -> lifecycle_transition). Trace attributes get the
prefix 'case_' (case_concept_name is the case ID). All values are written as text;
the app detects dates and numbers itself. The file is streamed, so large logs fit in memory.
"""
import gzip
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

SIMPLE = {"string", "date", "int", "float", "boolean", "id"}
CHUNK = 100_000


def _open(path):
    return gzip.open(path, "rb") if str(path).lower().endswith(".gz") else open(path, "rb")


def _tag(el):
    return el.tag.rsplit("}", 1)[-1]


def _col(key):
    return key.replace(":", "_").replace(" ", "_")


def _attrs(el):
    """Direct simple attributes of a trace or event element."""
    out = {}
    for child in el:
        t = _tag(child)
        if t in SIMPLE and "key" in child.attrib:
            out[_col(child.attrib["key"])] = child.attrib.get("value")
    return out


def _iter_rows(path):
    """One dict per event; trace attributes are collected for the whole trace first."""
    stack = []
    trace_attrs = {}
    pending = []
    for ev, el in ET.iterparse(_open(path), events=("start", "end")):
        t = _tag(el)
        if ev == "start":
            stack.append(t)
            if t == "trace":
                trace_attrs, pending = {}, []
            continue
        stack.pop()
        parent = stack[-1] if stack else None
        if t in SIMPLE and parent == "trace" and "key" in el.attrib:
            trace_attrs[_col(el.attrib["key"])] = el.attrib.get("value")
        elif t == "event" and parent == "trace":
            pending.append(_attrs(el))
            el.clear()
        elif t == "trace":
            for e in pending:
                row = {f"case_{k}": v for k, v in trace_attrs.items()}
                row.update(e)
                yield row
            el.clear()


def convert(src, dst):
    # pass 1: all column names (streamed)
    cols, n = {}, 0
    for row in _iter_rows(src):
        for k in row:
            cols.setdefault(k, None)
        n += 1
    order = [c for c in ("case_concept_name", "concept_name", "time_timestamp", "org_resource",
                         "lifecycle_transition") if c in cols] + sorted(
        c for c in cols if c not in {"case_concept_name", "concept_name", "time_timestamp",
                                     "org_resource", "lifecycle_transition"})
    schema = pa.schema([(c, pa.string()) for c in order])
    # pass 2: write in chunks
    writer = pq.ParquetWriter(dst, schema, compression="zstd")
    buf, done, traces = {c: [] for c in order}, 0, set()
    for row in _iter_rows(src):
        for c in order:
            buf[c].append(row.get(c))
        traces.add(row.get("case_concept_name"))
        done += 1
        if done % CHUNK == 0:
            writer.write_table(pa.table(buf, schema=schema))
            buf = {c: [] for c in order}
            print(f"  {done:,} / {n:,} events", flush=True)
    if order and buf[order[0]]:
        writer.write_table(pa.table(buf, schema=schema))
    writer.close()
    print(f"Done: {done:,} events, {len(traces):,} cases, {len(order)} columns -> {dst}")
    print("Map in the app: Case = case_concept_name, Activity = concept_name, "
          "Timestamp = time_timestamp, Resource = org_resource")
    if "lifecycle_transition" in cols:
        print("This log has lifecycle_transition: on page 1 use 'Only keep rows where this column...'"
              " = lifecycle_transition in [complete] (one row per finished step).")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1])
    name = src.name
    for ext in (".xes.gz", ".xes"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name(name + ".parquet")
    convert(src, dst)