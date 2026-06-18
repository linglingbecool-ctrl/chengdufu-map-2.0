#!/usr/bin/env python3
import argparse
import json
import math
import re
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path


ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC


class CFBError(RuntimeError):
    pass


class CompoundFile:
    def __init__(self, path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        if self.data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise CFBError("Not an OLE Compound File.")
        self.sector_size = 1 << struct.unpack_from("<H", self.data, 30)[0]
        self.mini_sector_size = 1 << struct.unpack_from("<H", self.data, 32)[0]
        self.first_dir_sector = struct.unpack_from("<I", self.data, 48)[0]
        self.mini_cutoff = struct.unpack_from("<I", self.data, 56)[0]
        self.first_mini_fat_sector = struct.unpack_from("<I", self.data, 60)[0]
        self.num_mini_fat_sectors = struct.unpack_from("<I", self.data, 64)[0]
        self.first_difat_sector = struct.unpack_from("<I", self.data, 68)[0]
        self.num_difat_sectors = struct.unpack_from("<I", self.data, 72)[0]
        self.fat = self._read_fat()
        self.dir_entries = self._read_directory()
        self.root = next((e for e in self.dir_entries if e["type"] == 5), None)
        self.mini_fat = self._read_mini_fat()
        self.mini_stream = b""
        if self.root and self.root["size"]:
            self.mini_stream = self._read_stream_from_fat(
                self.root["start"], self.root["size"]
            )

    def _sector_offset(self, sector):
        return 512 + sector * self.sector_size

    def _sector_bytes(self, sector):
        off = self._sector_offset(sector)
        return self.data[off : off + self.sector_size]

    def _sector_chain(self, start, table=None):
        if start in (ENDOFCHAIN, FREESECT):
            return []
        table = table or self.fat
        chain = []
        seen = set()
        cur = start
        while cur not in (ENDOFCHAIN, FREESECT):
            if cur in seen or cur >= len(table):
                raise CFBError("Broken sector chain.")
            seen.add(cur)
            chain.append(cur)
            cur = table[cur]
        return chain

    def _read_fat(self):
        difat = list(struct.unpack_from("<109I", self.data, 76))
        cur = self.first_difat_sector
        for _ in range(self.num_difat_sectors):
            if cur in (ENDOFCHAIN, FREESECT):
                break
            sec = self._sector_bytes(cur)
            entries = struct.unpack_from("<{}I".format(self.sector_size // 4), sec, 0)
            difat.extend(entries[:-1])
            cur = entries[-1]
        fat = []
        for sector in difat:
            if sector in (FREESECT, ENDOFCHAIN):
                continue
            sec = self._sector_bytes(sector)
            fat.extend(struct.unpack_from("<{}I".format(self.sector_size // 4), sec, 0))
        return fat

    def _read_stream_from_fat(self, start, size):
        chunks = [self._sector_bytes(s) for s in self._sector_chain(start)]
        return b"".join(chunks)[:size]

    def _read_directory(self):
        raw = self._read_stream_from_fat(self.first_dir_sector, 1 << 63)
        entries = []
        for off in range(0, len(raw), 128):
            ent = raw[off : off + 128]
            if len(ent) < 128:
                continue
            name_len = struct.unpack_from("<H", ent, 64)[0]
            if name_len >= 2:
                name = ent[: name_len - 2].decode("utf-16le", errors="replace")
            else:
                name = ""
            obj_type = ent[66]
            start = struct.unpack_from("<I", ent, 116)[0]
            size = struct.unpack_from("<Q", ent, 120)[0]
            entries.append({"name": name, "type": obj_type, "start": start, "size": size})
        return entries

    def _read_mini_fat(self):
        if self.first_mini_fat_sector in (ENDOFCHAIN, FREESECT):
            return []
        raw = self._read_stream_from_fat(
            self.first_mini_fat_sector,
            self.num_mini_fat_sectors * self.sector_size,
        )
        return list(struct.unpack_from("<{}I".format(len(raw) // 4), raw, 0))

    def _read_mini_stream(self, start, size):
        chunks = []
        for mini_sector in self._sector_chain(start, self.mini_fat):
            off = mini_sector * self.mini_sector_size
            chunks.append(self.mini_stream[off : off + self.mini_sector_size])
        return b"".join(chunks)[:size]

    def stream(self, name):
        wanted = name.casefold()
        entry = next((e for e in self.dir_entries if e["name"].casefold() == wanted), None)
        if not entry:
            raise CFBError(f"Stream not found: {name}")
        if entry["size"] < self.mini_cutoff and self.mini_stream:
            return self._read_mini_stream(entry["start"], entry["size"])
        return self._read_stream_from_fat(entry["start"], entry["size"])


@dataclass
class Record:
    sid: int
    data: bytes
    offset: int


def iter_records(blob, start=0):
    pos = start
    size = len(blob)
    while pos + 4 <= size:
        sid, length = struct.unpack_from("<HH", blob, pos)
        data = blob[pos + 4 : pos + 4 + length]
        if pos + 4 + length > size:
            break
        yield Record(sid, data, pos)
        pos += 4 + length


class SegmentReader:
    def __init__(self, segments):
        self.segments = segments
        self.seg = 0
        self.pos = 0

    def remaining_in_segment(self):
        if self.seg >= len(self.segments):
            return 0
        return len(self.segments[self.seg]) - self.pos

    def read(self, n):
        out = bytearray()
        while n:
            if self.seg >= len(self.segments):
                raise EOFError("Unexpected end of SST.")
            avail = self.remaining_in_segment()
            if avail == 0:
                self.seg += 1
                self.pos = 0
                continue
            take = min(avail, n)
            out.extend(self.segments[self.seg][self.pos : self.pos + take])
            self.pos += take
            n -= take
        return bytes(out)

    def skip(self, n):
        self.read(n)

    def read_chars(self, cch, high_byte):
        parts = []
        remaining = cch
        current_high = high_byte
        while remaining:
            if self.seg >= len(self.segments):
                raise EOFError("Unexpected end of SST character data.")
            avail = self.remaining_in_segment()
            if avail == 0:
                self.seg += 1
                self.pos = 0
                if self.seg >= len(self.segments):
                    raise EOFError("Missing SST continuation flags.")
                current_high = bool(self.read(1)[0] & 0x01)
                avail = self.remaining_in_segment()
            width = 2 if current_high else 1
            chars_here = min(remaining, avail // width)
            if chars_here == 0:
                self.seg += 1
                self.pos = 0
                if self.seg < len(self.segments):
                    current_high = bool(self.read(1)[0] & 0x01)
                continue
            raw = self.read(chars_here * width)
            enc = "utf-16le" if current_high else "latin1"
            parts.append(raw.decode(enc, errors="replace"))
            remaining -= chars_here
        return "".join(parts)


def parse_xl_unicode_short(data, pos):
    cch = data[pos]
    pos += 1
    flags = data[pos]
    pos += 1
    high = bool(flags & 0x01)
    byte_len = cch * (2 if high else 1)
    raw = data[pos : pos + byte_len]
    pos += byte_len
    enc = "utf-16le" if high else "latin1"
    return raw.decode(enc, errors="replace"), pos


def parse_sst(records, index):
    first = records[index].data
    segments = [first]
    pos = index + 1
    while pos < len(records) and records[pos].sid == 0x003C:
        segments.append(records[pos].data)
        pos += 1
    reader = SegmentReader(segments)
    _total = struct.unpack("<I", reader.read(4))[0]
    unique = struct.unpack("<I", reader.read(4))[0]
    strings = []
    for _ in range(unique):
        cch = struct.unpack("<H", reader.read(2))[0]
        flags = reader.read(1)[0]
        high = bool(flags & 0x01)
        has_ext = bool(flags & 0x04)
        has_rich = bool(flags & 0x08)
        rich_runs = struct.unpack("<H", reader.read(2))[0] if has_rich else 0
        ext_size = struct.unpack("<I", reader.read(4))[0] if has_ext else 0
        text = reader.read_chars(cch, high)
        if rich_runs:
            reader.skip(rich_runs * 4)
        if ext_size:
            reader.skip(ext_size)
        strings.append(text)
    return strings, pos


def rk_value(raw):
    rk = struct.unpack("<I", raw)[0]
    mult = 0.01 if (rk & 0x01) else 1.0
    if rk & 0x02:
        value = rk >> 2
        if value & (1 << 29):
            value -= 1 << 30
        return value * mult
    as_int64 = (rk & 0xFFFFFFFC) << 32
    return struct.unpack("<d", struct.pack("<Q", as_int64))[0] * mult


def clean_number(value):
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        if value.is_integer() and abs(value) < 9007199254740992:
            return int(value)
    return value


def parse_workbook(blob):
    records = list(iter_records(blob))
    sheets = []
    sst = []
    i = 0
    while i < len(records):
        rec = records[i]
        if rec.sid == 0x0085 and len(rec.data) >= 8:
            sheet_offset = struct.unpack_from("<I", rec.data, 0)[0]
            visible_state = rec.data[4]
            sheet_type = rec.data[5]
            name, _ = parse_xl_unicode_short(rec.data, 6)
            if sheet_type == 0:
                sheets.append(
                    {"name": name, "offset": sheet_offset, "visible_state": visible_state}
                )
        elif rec.sid == 0x00FC:
            sst, i = parse_sst(records, i)
            continue
        i += 1

    for sheet in sheets:
        sheet["cells"] = parse_sheet(blob, sheet["offset"], sst)
    return sheets


def parse_formula_value(raw8):
    if len(raw8) < 8:
        return None
    if raw8[6:8] == b"\xff\xff":
        marker = raw8[0]
        if marker == 0:
            return None
        if marker == 1:
            return bool(raw8[2])
        if marker == 2:
            return None
        if marker == 3:
            return ""
    return clean_number(struct.unpack("<d", raw8)[0])


def parse_sheet(blob, offset, sst):
    cells = {}
    for rec in iter_records(blob, offset):
        sid, data = rec.sid, rec.data
        if sid == 0x000A:
            break
        if sid == 0x0203 and len(data) >= 14:
            row, col = struct.unpack_from("<HH", data, 0)
            cells[(row, col)] = clean_number(struct.unpack_from("<d", data, 6)[0])
        elif sid == 0x00FD and len(data) >= 10:
            row, col = struct.unpack_from("<HH", data, 0)
            idx = struct.unpack_from("<I", data, 6)[0]
            cells[(row, col)] = sst[idx] if idx < len(sst) else None
        elif sid == 0x027E and len(data) >= 10:
            row, col = struct.unpack_from("<HH", data, 0)
            cells[(row, col)] = clean_number(rk_value(data[6:10]))
        elif sid == 0x00BD and len(data) >= 6:
            row, col_first, col_last = struct.unpack_from("<HHH", data, 0)
            pos = 6
            for col in range(col_first, col_last + 1):
                if pos + 6 > len(data):
                    break
                cells[(row, col)] = clean_number(rk_value(data[pos + 2 : pos + 6]))
                pos += 6
        elif sid == 0x0204 and len(data) >= 8:
            row, col = struct.unpack_from("<HH", data, 0)
            cch = struct.unpack_from("<H", data, 6)[0]
            raw = data[8 : 8 + cch]
            cells[(row, col)] = raw.decode("cp936", errors="replace")
        elif sid == 0x0006 and len(data) >= 14:
            row, col = struct.unpack_from("<HH", data, 0)
            cells[(row, col)] = parse_formula_value(data[6:14])
        elif sid == 0x0205 and len(data) >= 8:
            row, col = struct.unpack_from("<HH", data, 0)
            flag = data[6]
            value = data[7]
            cells[(row, col)] = bool(value) if flag == 0 else None
    return cells


def value_kind(value):
    if value is None:
        return "blank"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "real"
    return "text"


def table_name(name, used):
    base = re.sub(r"\W+", "_", name, flags=re.UNICODE).strip("_")
    if not base:
        base = "sheet"
    if re.match(r"^\d", base):
        base = "sheet_" + base
    candidate = base
    n = 2
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    used.add(candidate)
    return candidate


def column_name(value, index, used):
    text = "" if value is None else str(value).strip()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^\w]", "_", text, flags=re.UNICODE).strip("_")
    if not text:
        text = f"col_{index + 1:03d}"
    if re.match(r"^\d", text):
        text = "col_" + text
    candidate = text
    n = 2
    while candidate in used:
        candidate = f"{text}_{n}"
        n += 1
    used.add(candidate)
    return candidate


def dense_rows(cells):
    if not cells:
        return []
    max_row = max(r for r, _ in cells)
    max_col = max(c for _, c in cells)
    rows = []
    for r in range(max_row + 1):
        row = [cells.get((r, c)) for c in range(max_col + 1)]
        rows.append(row)
    return rows


def non_empty_count(row):
    return sum(v not in (None, "") for v in row)


def infer_header_row(rows):
    for idx, row in enumerate(rows[:20]):
        if non_empty_count(row) >= 2:
            return idx
    return 0


def sqlite_type(values):
    kinds = {value_kind(v) for v in values if v not in (None, "")}
    if not kinds:
        return "TEXT"
    if kinds <= {"integer"}:
        return "INTEGER"
    if kinds <= {"integer", "real"}:
        return "REAL"
    if kinds <= {"boolean"}:
        return "INTEGER"
    return "TEXT"


def write_database(sheets, db_path):
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE sheets (sheet_id INTEGER PRIMARY KEY, sheet_name TEXT NOT NULL, table_name TEXT NOT NULL, row_count INTEGER, column_count INTEGER, header_row INTEGER, visible_state INTEGER)"
    )
    conn.execute(
        "CREATE TABLE cells (sheet_id INTEGER, sheet_name TEXT, row_index INTEGER, column_index INTEGER, value TEXT, value_type TEXT)"
    )
    used_tables = set()
    summary = {"database": str(db_path), "sheets": []}
    for sheet_id, sheet in enumerate(sheets, 1):
        rows = dense_rows(sheet["cells"])
        header_idx = infer_header_row(rows) if rows else 0
        table = table_name(sheet["name"], used_tables)
        col_count = len(rows[0]) if rows else 0
        row_count = len(rows)
        conn.execute(
            "INSERT INTO sheets VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sheet_id,
                sheet["name"],
                table,
                row_count,
                col_count,
                header_idx + 1 if rows else None,
                sheet["visible_state"],
            ),
        )
        for (row, col), value in sorted(sheet["cells"].items()):
            conn.execute(
                "INSERT INTO cells VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sheet_id,
                    sheet["name"],
                    row + 1,
                    col + 1,
                    None if value is None else str(value),
                    value_kind(value),
                ),
            )

        if rows:
            headers = rows[header_idx]
            used_cols = set()
            columns = [column_name(headers[i], i, used_cols) for i in range(col_count)]
            data_rows = rows[header_idx + 1 :]
            types = [
                sqlite_type([row[i] for row in data_rows if i < len(row)])
                for i in range(col_count)
            ]
            col_defs = ", ".join(
                [f'"{columns[i]}" {types[i]}' for i in range(col_count)]
            )
            conn.execute(f'CREATE TABLE "{table}" (_row_index INTEGER, {col_defs})')
            placeholders = ", ".join(["?"] * (col_count + 1))
            quoted_cols = ", ".join(['"_row_index"'] + [f'"{c}"' for c in columns])
            for excel_row_index, row in enumerate(data_rows, header_idx + 2):
                values = [row[i] if i < len(row) else None for i in range(col_count)]
                if all(v in (None, "") for v in values):
                    continue
                values = [int(v) if isinstance(v, bool) else v for v in values]
                conn.execute(
                    f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({placeholders})',
                    [excel_row_index] + values,
                )
            inserted = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        else:
            columns = []
            inserted = 0

        summary["sheets"].append(
            {
                "sheet_id": sheet_id,
                "sheet_name": sheet["name"],
                "table_name": table,
                "rows_in_excel_area": row_count,
                "columns_in_excel_area": col_count,
                "header_row": header_idx + 1 if rows else None,
                "data_rows_inserted": inserted,
                "columns": columns,
            }
        )
    conn.execute("CREATE INDEX idx_cells_sheet_row_col ON cells(sheet_id, row_index, column_index)")
    conn.commit()
    conn.close()
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_xls")
    parser.add_argument("output_db")
    parser.add_argument("--summary-json", required=True)
    args = parser.parse_args()

    cfb = CompoundFile(args.input_xls)
    stream_name = "Workbook"
    try:
        workbook = cfb.stream(stream_name)
    except CFBError:
        stream_name = "Book"
        workbook = cfb.stream(stream_name)
    sheets = parse_workbook(workbook)
    summary = write_database(sheets, args.output_db)
    summary["source"] = str(Path(args.input_xls))
    summary["workbook_stream"] = stream_name
    Path(args.summary_json).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
