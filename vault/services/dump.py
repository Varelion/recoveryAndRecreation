"""Flat-file export of every table and analytics view for external tools
(pandas, DuckDB, Power BI, spreadsheets)."""

import csv
import json
from enum import Enum
from pathlib import Path

from vault.store import Store


class Format(Enum):
    CSV = "csv"
    JSONL = "jsonl"


async def dump(store: Store, out: Path, fmt: Format) -> dict[str, int]:
    """Write one file per table/view. Returns row counts."""
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for table in await store.tables():
        counts[table] = await _table(store, table, out / f"{table}.{fmt.value}", fmt)

    return counts


async def _table(store: Store, table: str, path: Path, fmt: Format) -> int:
    rows = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        async for names, row in store.scan(table):
            if fmt is Format.JSONL:
                fh.write(json.dumps(dict(zip(names, row)), ensure_ascii=False) + "\n")
                rows += 1
                continue

            if rows == 0:
                writer.writerow(names)
            writer.writerow(row)
            rows += 1

    return rows
