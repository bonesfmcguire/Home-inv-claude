#!/usr/bin/env python3
"""Home Inventory - a tiny, dependency-free home inventory server.

Runs on the Python standard library only (SQLite + http.server), so it works
as a Home Assistant add-on, a Docker container, or just `python3 server.py`.
"""
import calendar
import csv
import difflib
import io
import json
import os
import re
import secrets
import sqlite3
import threading
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
PORT = int(os.environ.get("PORT", "8099"))
STATIC = Path(__file__).parent / "static"
DATA_DIR.mkdir(parents=True, exist_ok=True)
PHOTO_DIR = DATA_DIR / "photos"
PHOTO_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "inventory.db"
LOCK = threading.Lock()
MAX_BODY = 16 * 1024 * 1024

FIELDS = (
    "name", "quantity", "location", "stored", "category", "notes",
    "price", "purchase_date", "purchased_from", "brand", "serial", "warranty_until",
)
TEXT_FIELDS = ("category", "notes", "purchased_from", "brand", "serial")
DATE_FIELDS = ("purchase_date", "warranty_until")
# Columns added after v1.0 - applied to existing databases on startup.
EXTRA_COLS = {
    "price": "REAL",
    "purchase_date": "TEXT NOT NULL DEFAULT ''",
    "purchased_from": "TEXT NOT NULL DEFAULT ''",
    "brand": "TEXT NOT NULL DEFAULT ''",
    "serial": "TEXT NOT NULL DEFAULT ''",
    "warranty_until": "TEXT NOT NULL DEFAULT ''",
    "photo": "TEXT NOT NULL DEFAULT ''",
}

# --------------------------------------------------------------------------- db


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                location TEXT NOT NULL DEFAULT '',
                stored INTEGER NOT NULL DEFAULT 0,
                category TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL)"""
        )
        have = {r["name"] for r in c.execute("PRAGMA table_info(items)")}
        for col, ddl in EXTRA_COLS.items():
            if col not in have:
                c.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")
        tidy_locations(c)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row(r):
    d = dict(r)
    d["stored"] = bool(d["stored"])
    return d


def truthy(v):
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in {"1", "true", "yes", "y", "x", "stored", "storage", "packed", "✓", "✔"}


def to_int(v, default=1):
    try:
        return max(0, int(float(str(v).strip())))
    except (TypeError, ValueError):
        return default


def parse_price(v):
    """'$1,299.99' -> 1299.99; blank/garbage -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return round(max(float(v), 0), 2)
    m = re.search(r"\d+(?:\.\d+)?", str(v).replace(",", ""))
    return round(float(m[0]), 2) if m else None


_MONTHS = {}
for _i, _n in enumerate(calendar.month_name):
    if _n:
        _MONTHS[_n.lower()] = _i
        _MONTHS[_n[:3].lower()] = _i
_MONTHS["sept"] = 9


def parse_date(v):
    """Return 'YYYY-MM-DD', '' for blank, or None if the text isn't a date we understand."""
    s = str(v or "").strip().strip(".,")
    if not s:
        return ""
    low = s.lower()
    today = date.today()
    y = mo = d = None
    if low == "today":
        return today.isoformat()
    if low == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    if m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s):
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    elif m := re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", s):
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        mo, d = (b, a) if a > 12 else (a, b)  # US order unless it can't be a month
        y += 2000 if y < 100 else 0
    elif m := re.fullmatch(r"([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", low):
        mo, d, y = _MONTHS.get(m[1]), int(m[2]), int(m[3])
    elif m := re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?,?\s+(\d{4})", low):
        d, mo, y = int(m[1]), _MONTHS.get(m[2]), int(m[3])
    elif m := re.fullmatch(r"([a-z]{3,9})\.?,?\s+(\d{4})", low):
        mo, d, y = _MONTHS.get(m[1]), 1, int(m[2])
    elif m := re.fullmatch(r"(\d{4})", s):
        y, mo, d = int(m[1]), 1, 1
    if not (y and mo and d):
        return None
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def clean(data, partial=False):
    out = {}
    for f in FIELDS:
        if f not in data:
            continue
        v = data[f]
        if f == "quantity":
            out[f] = to_int(v)
        elif f == "stored":
            out[f] = 1 if truthy(v) else 0
        elif f == "price":
            out[f] = parse_price(v)
        elif f in DATE_FIELDS:
            d = parse_date(v)
            if d is None:
                raise ValueError(f"I couldn't read the date “{v}”. Use YYYY-MM-DD.")
            out[f] = d
        else:
            out[f] = re.sub(r"\s+", " ", str(v or "")).strip()
    if not partial:
        if not out.get("name"):
            raise ValueError("Item needs a name")
        out.setdefault("quantity", 1)
        out.setdefault("stored", 0)
        out.setdefault("price", None)
        for f in TEXT_FIELDS + DATE_FIELDS + ("location",):
            out.setdefault(f, "")
    elif "name" in out and not out["name"]:
        raise ValueError("Item needs a name")
    return out


# ------------------------------------------------------- locations (any casing)

SEP = re.compile(r"\s*[/>»›]\s*")


def segs(loc):
    """'kitchen /  Junk drawer' -> ['kitchen', 'Junk drawer']"""
    return [re.sub(r"\s+", " ", p).strip() for p in SEP.split(loc or "") if p.strip()]


def canon_map(c, exclude=None):
    """lower-case path segment -> the spelling most items already use."""
    counts = {}
    for (loc,) in c.execute("SELECT location FROM items WHERE location<>'' AND id IS NOT ?", (exclude,)):
        for p in segs(loc):
            counts.setdefault(p.lower(), Counter())[p] += 1
    return {
        k: min(v, key=lambda s: (-v[s], -sum(ch.isupper() for ch in s), s))  # most used, then more capitals
        for k, v in counts.items()
    }


def canon_location(c, loc, cmap=None, exclude=None):
    parts = segs(loc)
    if not parts:
        return ""
    cmap = canon_map(c, exclude) if cmap is None else cmap
    return " / ".join(cmap.get(p.lower()) or cap(p) for p in parts)


def canon_batch(c, items):
    """Normalise locations across a whole batch (first spelling wins within the batch)."""
    cmap = canon_map(c)
    for it in items:
        it["location"] = " / ".join(cmap.setdefault(p.lower(), cap(p)) for p in segs(it.get("location")))
    return items


def tidy_locations(c):
    """One-time-safe clean-up: 'Living room' + 'Living Room' -> one spelling."""
    cmap = canon_map(c)
    for (loc,) in c.execute("SELECT DISTINCT location FROM items WHERE location<>''").fetchall():
        new = canon_location(c, loc, cmap)
        if new != loc:
            c.execute("UPDATE items SET location=? WHERE location=?", (new, loc))


def rename_location(c, old, new):
    """Rename a place (and everything nested under it), whatever casing it was stored in."""
    o = [p.lower() for p in segs(old)]
    n = segs(new)
    if not o:
        raise ValueError("Which location?")
    if not n:
        raise ValueError("The new name can't be empty")
    moved = 0
    for r in c.execute("SELECT id, location FROM items").fetchall():
        p = segs(r["location"])
        if [x.lower() for x in p[: len(o)]] == o:
            c.execute("UPDATE items SET location=?, updated_at=? WHERE id=?", (" / ".join(n + p[len(o):]), now(), r["id"]))
            moved += 1
    return {"moved": moved}


# ------------------------------------------------------------------------ items


def all_items(c):
    return [row(r) for r in c.execute("SELECT * FROM items ORDER BY location COLLATE NOCASE, name COLLATE NOCASE")]


def get_item(c, item_id):
    r = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return row(r) if r else None


def add_item(c, data):
    """Add an item, merging quantity into an identical name+location entry."""
    d = clean(data)
    d["location"] = canon_location(c, d["location"])
    ex = c.execute(
        "SELECT * FROM items WHERE lower(name)=lower(?) AND lower(location)=lower(?)",
        (d["name"], d["location"]),
    ).fetchone()
    if ex:
        upd = {"quantity": ex["quantity"] + d["quantity"], "updated_at": now()}
        if d["stored"]:
            upd["stored"] = 1
        for f in TEXT_FIELDS + DATE_FIELDS:
            if d[f] and not ex[f]:
                upd[f] = d[f]
        if d["price"] is not None and ex["price"] is None:
            upd["price"] = d["price"]
        sets = ", ".join(f"{k}=?" for k in upd)
        c.execute(f"UPDATE items SET {sets} WHERE id=?", (*upd.values(), ex["id"]))
        return get_item(c, ex["id"])
    ts = now()
    cols = list(FIELDS)
    cur = c.execute(
        f"INSERT INTO items({','.join(cols)},created_at,updated_at) VALUES({','.join('?' * (len(cols) + 2))})",
        (*[d[f] for f in cols], ts, ts),
    )
    return get_item(c, cur.lastrowid)


def update_item(c, item_id, data):
    d = clean(data, partial=True)
    if not d:
        return get_item(c, item_id)
    if "location" in d:
        d["location"] = canon_location(c, d["location"], exclude=item_id)
    d["updated_at"] = now()
    sets = ", ".join(f"{k}=?" for k in d)
    c.execute(f"UPDATE items SET {sets} WHERE id=?", (*d.values(), item_id))
    return get_item(c, item_id)


def delete_item(c, item_id):
    it = get_item(c, item_id)
    c.execute("DELETE FROM items WHERE id=?", (item_id,))
    if it:
        drop_photo_files(it["photo"])


def stats(c):
    r = c.execute(
        "SELECT count(*) n, coalesce(sum(quantity),0) q, coalesce(sum(stored),0) s,"
        " coalesce(sum(price*quantity),0) v FROM items"
    ).fetchone()
    locs = {l.lower() for (l,) in c.execute("SELECT location FROM items WHERE location<>''")}
    return {"items": r["n"], "quantity": r["q"], "locations": len(locs), "stored": r["s"], "value": round(r["v"], 2)}


# ---------------------------------------------------------------------- photos


def sniff_image(b):
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def thumb_name(name):
    return re.sub(r"\.\w+$", "", name) + "_t.jpg"


def drop_photo_files(name):
    if name:
        for n in (name, thumb_name(name)):
            (PHOTO_DIR / n).unlink(missing_ok=True)


def set_photo(c, item_id, data, thumb=False):
    ext = sniff_image(data or b"")
    if not ext:
        raise ValueError("That doesn't look like a JPEG, PNG, WebP or GIF image.")
    it = get_item(c, item_id)
    if thumb:
        if not it["photo"] or ext != "jpg":
            raise ValueError("Thumbnails must be JPEGs uploaded after the photo itself.")
        (PHOTO_DIR / thumb_name(it["photo"])).write_bytes(data)
        return it
    name = f"{item_id}-{secrets.token_hex(4)}.{ext}"
    (PHOTO_DIR / name).write_bytes(data)
    drop_photo_files(it["photo"])
    c.execute("UPDATE items SET photo=?, updated_at=? WHERE id=?", (name, now(), item_id))
    return get_item(c, item_id)


def clear_photo(c, item_id):
    it = get_item(c, item_id)
    drop_photo_files(it["photo"])
    c.execute("UPDATE items SET photo='', updated_at=? WHERE id=?", (now(), item_id))
    return get_item(c, item_id)


# ------------------------------------------------------------ language parsing

NUM_WORDS = [
    ("a couple of", 2), ("a couple", 2), ("couple of", 2), ("a dozen", 12), ("a few", 3),
    ("dozen", 12), ("one", 1), ("two", 2), ("three", 3), ("four", 4), ("five", 5), ("six", 6),
    ("seven", 7), ("eight", 8), ("nine", 9), ("ten", 10), ("eleven", 11), ("twelve", 12),
    ("twenty", 20), ("an", 1), ("a", 1), ("some", 1),
]
LOC_SPLIT = re.compile(
    r"\s+(?:in|on|at|inside|into|under|underneath|behind|beside|next to|on top of)\s+(?:the\s+|my\s+|our\s+)?"
    r"|\s*@\s*|\s+[-–—]\s+",
    re.I,
)
ARTICLES = re.compile(r"^(?:the|my|our|some|any|of|a|an)\s+", re.I)

PREP = r"(?:in|on|at|inside|into|under|underneath|behind|beside|next to)"
LEAD_PUT = re.compile(
    r"(?:,|\s+and|\s+then)?\s+(?:i\s+)?(?:put|stored?|placed?|kept|left)\s+(?:it|them)\s+(?=" + PREP + r"\b)", re.I
)
PRICE_RE = re.compile(
    r"(?:\b(?:for|at|paid|cost|costs|costing|priced at|price)\s+)?"
    r"(?:\$\s?(\d[\d,]*(?:\.\d+)?)|\b(\d[\d,]*(?:\.\d+)?)\s*(?:dollars|bucks|usd)\b)(?:\s+(?:each|ea)\b)?",
    re.I,
)
_D = r"(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})"
WARRANTY_RE = re.compile(r"[,;]?\s*\bwarranty\s+(?:until|through|expires?(?:\s+on)?|ends?|to|till)\s+" + _D, re.I)
DATE_RE = re.compile(r"(?:\bon\s+)?(?<![\w/-])" + _D + r"(?![\w/-])", re.I)
TODAY_RE = re.compile(r"(?:\b(?:bought|purchased|got|ordered)\s+(?:it\s+|them\s+)?)?\b(today|yesterday)\b", re.I)
FROM_RE = re.compile(
    r"[,;]?\s*\b(?:(?:bought|purchased|got|ordered)\s+(?:it\s+|them\s+)?)?from\s+(.+?)"
    r"(?=\s+(?:for|in|on|at|inside|into|under|behind|beside|next to)\b|\s*[,;(]|\s*$)",
    re.I,
)
# comma-separated pieces that describe the previous item rather than being a new item
MODIFIER_RE = re.compile(
    r"^(?:(?:is |are )?(?:stored|packed away|in storage|storage)"
    r"|warranty\b.*|(?:from|paid|costs?|bought|purchased)\s.*|for\s+\$.*|\$.*"
    r"|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})$",
    re.I,
)
_NEW_ITEM = r"(?=(?:\d|(?:a|an|the|some|my|our|one|two|three|four|five|six|seven|eight|nine|ten|dozen|couple)\b))"
_NOT_PUT = r"(?!(?:i\s+)?(?:put|stored?|placed?|kept|left)\b)"


def cap(s):
    s = s.strip()
    return s[:1].upper() + s[1:] if s else s


def strip_articles(s):
    prev = None
    while prev != s:
        prev, s = s, ARTICLES.sub("", s.strip())
    return s.strip()


def take_quantity(name):
    """'3 AA batteries' -> (3, 'AA batteries'); also 'batteries x3', 'two hammers'."""
    name = name.strip()
    m = re.match(r"^(\d+)\s*[x×]?\s+(.+)$", name)
    if m:
        return int(m[1]), m[2]
    m = re.match(r"^(.+?)\s*(?:[x×]\s*(\d+)|\((\d+)\))$", name)
    if m:
        return int(m[2] or m[3]), m[1]
    low = name.lower()
    for word, n in NUM_WORDS:
        if low.startswith(word + " "):
            return n, name[len(word) + 1:]
    return None, name


def parse_phrase(text, default_location="", hint_buy=False):
    """Turn '3 AA batteries in the kitchen drawer' into an item dict.

    Also understands: '$89' / 'for $89 each' (price), 'from Home Depot' (when buying),
    '2024-03-01' or 'today' (purchase date), 'warranty until 2027-01-01'.
    """
    t = re.sub(r"\s+", " ", str(text)).strip().strip(".!?,;")
    extra = {}
    stored = False
    if m := LEAD_PUT.search(t):
        stored = bool(re.search(r"stor", m[0], re.I))
        t = LEAD_PUT.sub(" ", t, count=1)
    stored = stored or bool(re.search(r"\b(stored|storage|packed away)\b", t, re.I))
    t = re.sub(r"[,;]?\s*\b(?:is |are )?(?:stored|packed away)\b[,;]?\s*", " ", t, flags=re.I).strip(" ,;")

    if m := PRICE_RE.search(t):
        extra["price"] = parse_price(m[1] or m[2])
        t = t[: m.start()] + " " + t[m.end():]
    if m := WARRANTY_RE.search(t):
        extra["warranty_until"] = parse_date(m[1]) or ""
        t = t[: m.start()] + " " + t[m.end():]
    if m := DATE_RE.search(t):
        extra["purchase_date"] = parse_date(m[1]) or ""
        t = t[: m.start()] + " " + t[m.end():]
    elif hint_buy and (m := TODAY_RE.search(t)):
        extra["purchase_date"] = parse_date(m[1])
        t = t[: m.start()] + " " + t[m.end():]
    if (hint_buy or extra.get("price") is not None) and (m := FROM_RE.search(t)):
        store = strip_articles(m[1]).strip(" ,;")
        extra["purchased_from"] = store if store != store.lower() else store.title()
        t = t[: m.start()] + " " + t[m.end():]
    t = re.sub(r"\s+([,;])", r"\1", t)
    t = re.sub(r"^(?:and\s+)+|(?:\s+and)+$", "", re.sub(r"\s{2,}", " ", t).strip(" ,;"), flags=re.I).strip(" ,;")

    parts = LOC_SPLIT.split(t, maxsplit=1)
    name, location = parts[0], (parts[1] if len(parts) > 1 else default_location)
    qty, name = take_quantity(name)
    name = strip_articles(name).strip(" ,;")
    item = {
        "name": cap(name),
        "quantity": qty or 1,
        "location": cap(strip_articles(location or "").strip(" ,;")),
        "stored": stored,
    }
    item.update({k: v for k, v in extra.items() if v not in (None, "")})
    return item


def split_pieces(s):
    """'3 batteries, a tent and 2 tarps in the garage' -> three pieces.

    Splits on commas / semicolons, and on 'and' when it's clearly between two items.
    """
    parts = [p for p in re.split(r"\s*(?:;|(?<!\d),|,(?!\d))\s*", s) if p.strip()]
    merged = []
    for p in parts:
        if merged and MODIFIER_RE.match(p.strip()):
            merged[-1] += ", " + p.strip()  # 'tent in the attic, stored'
        else:
            merged.append(p)
    listy = len(merged) > 1
    pat = r"\s+and\s+" + _NOT_PUT + ("" if listy else _NEW_ITEM)
    pieces = []
    for p in merged:
        pieces.extend(re.split(pat, p, flags=re.I))
    return [p.strip(" ,;") for p in pieces if p.strip(" ,;")]


def parse_list_line(line, default_loc="", hint_buy=False):
    """One line holding one or more items. Items without a place inherit the next stated one."""
    items = [i for i in (parse_phrase(p, "", hint_buy) for p in split_pieces(line)) if i["name"]]
    nxt = default_loc
    for it in reversed(items):
        if it["location"]:
            nxt = it["location"]
        else:
            it["location"] = nxt
    return items


HEADER_ALIASES = {
    "name": {"name", "item", "items", "thing", "what", "description", "item name", "object"},
    "quantity": {"quantity", "qty", "count", "amount", "#", "number", "how many", "units"},
    "location": {"location", "where", "place", "room", "spot", "area", "loc", "storage location", "bin", "box"},
    "stored": {"stored", "in storage", "storage", "packed", "is stored", "stored?"},
    "category": {"category", "type", "kind", "tag", "tags", "group"},
    "notes": {"notes", "note", "comments", "comment", "details", "info"},
    "price": {"price", "cost", "paid", "price paid", "amount paid", "unit price", "price (each)", "value"},
    "purchase_date": {"purchase date", "purchased", "date purchased", "date", "acquired", "date acquired",
                      "bought", "purchase_date", "acquired on", "purchased on", "bought on"},
    "purchased_from": {"purchased from", "store", "vendor", "seller", "retailer", "bought at", "bought from",
                       "source", "from", "shop", "purchased_from"},
    "brand": {"brand", "make", "manufacturer", "brand / model", "brand/model", "model"},
    "serial": {"serial", "serial number", "serial #", "serial no", "sn", "s/n"},
    "warranty_until": {"warranty", "warranty until", "warranty expires", "warranty end", "warranty expiry",
                       "warranty_until", "warranty expiration"},
}
DEFAULT_COLUMNS = ["name", "location", "quantity", "category", "notes"]


def map_header(cells):
    mapping = {}
    for i, cell in enumerate(cells):
        key = cell.strip().lower()
        for field, aliases in HEADER_ALIASES.items():
            if key in aliases and field not in mapping.values():
                mapping[i] = field
                break
    return mapping if "name" in mapping.values() else None


def row_to_item(d):
    d = dict(d)
    for f in DATE_FIELDS:  # an unreadable date shouldn't sink the whole import - keep it in the notes
        v = str(d.get(f, "") or "").strip()
        if v and parse_date(v) is None:
            d["notes"] = (str(d.get("notes", "")) + f" ({f.replace('_', ' ')}: {v})").strip()
            d[f] = ""
    q = str(d.get("quantity", "")).strip()
    d["quantity"] = to_int(q, 1) if q else 1
    d["stored"] = truthy(d.get("stored"))
    it = clean(d)
    it["stored"] = bool(it["stored"])
    return it


def parse_import(text):
    """Accepts pasted spreadsheet cells (TSV), CSV with a header, or a plain list.

    Lists may use 'Location:' heading lines to set the location for the lines below,
    and each line may hold several items ('2 tarps, a tent and a shop vac').
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return []
    first = lines[0]
    delim = None
    if "\t" in first:
        delim = "\t"
    elif "," in first and map_header(next(csv.reader([first]))):
        delim = ","
    elif ";" in first and map_header(first.split(";")):
        delim = ";"

    if delim:
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
        mapping = map_header(rows[0])
        if mapping:
            rows = rows[1:]
        else:
            mapping = dict(enumerate(DEFAULT_COLUMNS))
        items = []
        for r in rows:
            d = {f: r[i] for i, f in mapping.items() if i < len(r)}
            if str(d.get("name", "")).strip():
                items.append(row_to_item(d))
        return items

    items = []
    current_loc = ""
    for line in lines:
        s = re.sub(r"^\s*(?:[-*•·▪◦]|\d+[.)]|\[[ xX]?\])\s*", "", line).strip()
        s = re.sub(r"^(?:please\s+)?add\s+", "", s, flags=re.I)
        if not s:
            continue
        if s.endswith(":") or re.match(r"^#+\s", s):
            current_loc = cap(strip_articles(s.strip("#: ")))
            continue
        items.extend(parse_list_line(s, current_loc))
    return items


# ------------------------------------------------------------- search & speech


def singular(w):
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and re.search(r"(ch|sh|ss|x|z)es$", w):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokens(s):
    return [singular(w) for w in re.findall(r"[a-z0-9]+", (s or "").lower())]


STOP = {"the", "my", "a", "an", "any", "some", "our", "of", "do", "i", "have", "is", "are", "there", "left"}


def score(term_toks, text):
    hay = tokens(text)
    if not term_toks or not hay:
        return 0
    joined = " ".join(hay)
    if " ".join(term_toks) == joined:
        return 100
    if " ".join(term_toks) in joined:
        return 85
    hit = 0
    for t in term_toks:
        if t in hay or any(h.startswith(t) and len(t) >= 3 for h in hay):
            hit += 1
        elif difflib.get_close_matches(t, hay, n=1, cutoff=0.8):
            hit += 0.8
    return 70 * hit / len(term_toks) if hit >= len(term_toks) * 0.8 else 0


def search(c, term, field="item"):
    toks = [t for t in tokens(term) if t not in STOP] or tokens(term)
    out = []
    for it in all_items(c):
        name_hit = True
        if field == "location":
            s = score(toks, it["location"])
        else:
            s = score(toks, it["name"])
            if not s:
                s, name_hit = 0.69 * score(toks, f"{it['category']} {it['brand']} {it['notes']}"), False
        if s >= 45:
            out.append((s, it, name_hit))
    if any(hit for _, _, hit in out):
        out = [o for o in out if o[2]]  # drop category/notes-only hits when names match
    out = [(s, it) for s, it, _ in out]
    out.sort(key=lambda x: (-x[0], x[1]["name"].lower()))
    return [it for _, it in out]


def spoken_location(loc):
    """'Kitchen / Junk drawer' -> 'the Junk drawer in the Kitchen'."""
    return " in ".join(f"the {p}" for p in reversed(segs(loc)))


def describe(it, with_loc=True):
    q = f"{it['quantity']} " if it["quantity"] != 1 else ""
    s = f"{q}{it['name']}"
    if with_loc:
        s += f" in {spoken_location(it['location'])}" if it["location"] else " (no location set)"
    if it["stored"]:
        s += ", stored"
    return s


def join_list(parts, limit=5):
    extra = len(parts) - limit
    parts = parts[:limit]
    if extra > 0:
        parts.append(f"{extra} more")
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def money(p):
    return f"${p:,.2f}".replace(".00", "")


def pretty_date(s):
    try:
        d = date.fromisoformat(s)
        return f"{d:%B} {d.day}, {d.year}"
    except ValueError:
        return s


def purchase_line(it):
    head = f"The {it['name']}"
    src = f"from {it['purchased_from']}" if it.get("purchased_from") else ""
    when = f"on {pretty_date(it['purchase_date'])}" if it.get("purchase_date") else ""
    bought = " ".join(x for x in (src, when) if x)
    if it.get("price") is not None:
        s = f"{head} cost {money(it['price'])}" + (" each" if it["quantity"] > 1 else "")
        s += f", bought {bought}" if bought else ""
    elif bought:
        s = f"{head} was bought {bought}"
    else:
        return f"I don't have purchase details for the {it['name']}."
    if it.get("warranty_until"):
        s += f". Warranty until {pretty_date(it['warranty_until'])}"
    return s + "."


def answer_purchase(c, term):
    res = search(c, term)
    if not res:
        return {"speech": f"I couldn't find {term} in your inventory.", "results": [], "term": term}
    known = [i for i in res if i["price"] is not None or i["purchased_from"] or i["purchase_date"]]
    shown = (known or res)[:3]
    return {"speech": " ".join(purchase_line(i) for i in shown), "results": res, "term": term}


def answer_item(c, term, how_many=False):
    res = search(c, term)
    if not res:
        return {"speech": f"I couldn't find {term} in your inventory.", "results": [], "term": term}
    if how_many and len(res) > 1:
        total = sum(i["quantity"] for i in res)
        speech = f"You have {total} in total: {join_list([describe(i) for i in res])}."
    elif len(res) == 1:
        speech = f"{cap(describe(res[0]))}."
    else:
        speech = f"I found {len(res)} matches: {join_list([describe(i) for i in res])}."
    return {"speech": speech, "results": res, "term": term}


def answer_location(c, term):
    if term.lower().strip() in {"storage", "stored"}:
        res = [i for i in all_items(c) if i["stored"]]
        label = "storage"
    else:
        res = search(c, term, field="location")
        locs = {i["location"].lower() for i in res}
        label = spoken_location(res[0]["location"]) if len(locs) == 1 else f"the {term}"
    if not res:
        return {"speech": f"I don't have anything recorded in {term}.", "results": [], "term": term}

    def rel(i):  # only mention the part of the location that wasn't asked about
        if label == "storage":
            return describe(dict(i, stored=False))
        parts = [p for p in segs(i["location"]) if score(tokens(term), p) < 45]
        return describe(dict(i, location=" / ".join(parts))) if parts else describe(i, with_loc=False)

    names = [rel(i) for i in res]
    return {"speech": f"In {label} you have {join_list(names, 8)}.", "results": res, "term": term, "kind": "location"}


def ask(c, text=None, item=None, location=None, about=None):
    if location:
        return answer_location(c, location)
    if item:
        if about == "purchase":
            return answer_purchase(c, strip_articles(item))
        return answer_item(c, strip_articles(item))
    t = re.sub(r"\s+", " ", (text or "")).strip().strip("?.! ").lower()
    t = re.sub(r"\b(what|where)'?s\b", r"\1 is", t)
    m = re.match(
        r"^how much (?:did|do|does|was|were|is|are)(?: i| we)?(?: pay| spend| paid)?(?: for| on)? "
        r"(?:the |my |our )?(.+?)(?: cost(?: me| us)?)?$",
        t,
    ) or re.match(r"^(?:where|when) did (?:i|we) (?:buy|get|purchase|order|bought) (?:the |my |our )?(.+)$", t)
    if m:
        return answer_purchase(c, strip_articles(m[1]))
    m = re.match(
        r"^(?:what(?: is| do i have| have i got| are| do we have| is there)?|list|show(?: me)?)"
        r"(?: everything| all| stuff| things)?(?: stored| kept)? (?:in|on|inside|at) (?:the |my |our )?(.+)$",
        t,
    )
    if m:
        return answer_location(c, m[1])
    m = re.match(r"^what (?:kind of |sort of )?(.+?) do (?:i|we) have$", t)
    if m:
        return answer_item(c, strip_articles(m[1]))
    how_many = bool(re.match(r"^how many\b", t))
    t = re.sub(
        r"^(?:where (?:is|are|did i put|do i keep|can i find)|where|do (?:i|we) have(?: any)?|how many"
        r"|find|locate|search(?: for)?|look up|have i got(?: any)?|is there(?: any)?|are there(?: any)?)\s+",
        "",
        t,
    )
    t = re.sub(r"\s+(?:do (?:i|we) have|have i got|are there|is there|left|stored|kept)$", "", t)
    return answer_item(c, strip_articles(t), how_many)


VERBS = {
    "ask": r"^(?:where|what|how many|how much|when did|do (?:i|we) have|have i got|find|locate|search|look up|is there|are there|list|show)\b",
    "move": r"^(?:i\s+)?(?:move|moved)\s+",
    "remove": r"^(?:i\s+)?(?:remove|removed|delete|deleted|toss|tossed|throw (?:away|out)|threw (?:away|out)|used up|use up|used|got rid of|get rid of|sold|gave away|donated|lost)\s+",
    "add": r"^(?:i\s+)?(?:add|added|put|store|stored|place|placed|keep|bought|purchased|ordered|received|picked up|got|have)\s+",
}


def add_many(c, items, dry_run=False):
    if dry_run:
        return {"action": "add", "dry_run": True, "items": canon_batch(c, items), "count": len(items)}
    if not items:
        raise ValueError("I didn't catch anything to add.")
    saved, spoken = [], []
    for it in items:
        s = add_item(c, it)
        saved.append(s)
        spoken.append(dict(it, location=s["location"]))
    locs = {s["location"] for s in saved}
    if len(saved) == 1:
        speech = f"Added {describe(spoken[0])}."
    elif len(locs) == 1 and next(iter(locs)):
        speech = f"Added {join_list([describe(i, False) for i in spoken], 8)} to {spoken_location(next(iter(locs)))}."
    elif locs == {""}:
        speech = f"Added {join_list([describe(i, False) for i in spoken], 8)} (no location set)."
    elif len(saved) > 4:
        speech = f"Added {len(saved)} items across {len(locs)} places."
    else:
        speech = f"Added {join_list([describe(i) for i in spoken], 8)}."
    return {"action": "add", "speech": speech, "results": saved, "term": saved[0]["name"], "count": len(saved)}


def command(c, text, action=None, dry_run=False):
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Say or type something first")
    if "\n" in raw and action in (None, "add"):  # pasted list or spreadsheet
        return add_many(c, parse_import(raw), dry_run)
    t = re.sub(r"\s+", " ", raw)
    t = re.sub(r"\b(what|where|who)'?s\b", r"\1 is", t, flags=re.I)
    low = t.lower()
    if not action:
        for a in ("ask", "move", "remove", "add"):
            if re.match(VERBS[a], low):
                action = a
                break
        else:
            listy = len(split_pieces(t)) > 1
            action = "ask" if t.endswith("?") or not (LOC_SPLIT.search(t) or listy) else "add"
    if dry_run and action != "add":
        return {"action": action, "dry_run": True}
    if action == "ask":
        return dict(ask(c, text=t), action="ask")

    stored_verb = bool(re.match(r"^(?:i\s+)?stored?\b", low))
    buy_verb = bool(re.match(r"^(?:i\s+)?(?:bought|purchased|ordered|received|picked up|got)\b", low))
    body = re.sub(VERBS.get(action, "^$"), "", t, flags=re.I).strip()
    body = re.sub(r"\s+(?:to|from|in) (?:the |my )?inventory$", "", body, flags=re.I)

    if action == "add":
        body = re.sub(r"\s+(?:to|into|onto)\s+", " in ", body, count=1, flags=re.I)
        items = parse_list_line(body, hint_buy=buy_verb)
        for it in items:
            it["stored"] = it["stored"] or stored_verb
        return add_many(c, items, dry_run)

    if action == "move":
        m = re.match(r"^(.+?)(?:\s+from\s+.+?)?\s+(?:to|into|onto|in|inside)\s+(?:the |my |our )?(.+)$", body, re.I)
        if not m:
            raise ValueError("Try: move the tent to the garage")
        name, dest = strip_articles(m[1]), cap(strip_articles(m[2]))
        res = search(c, name)
        if not res:
            return {"action": "move", "speech": f"I couldn't find {name} in your inventory.", "results": []}
        saved = update_item(c, res[0]["id"], {"location": dest})
        return {"action": "move", "speech": f"Moved {saved['name']} to {spoken_location(saved['location'])}.", "results": [saved], "term": saved["name"]}

    # remove
    loc = None
    m = re.match(r"^(.+?)\s+(?:from|in|on|at)\s+(?:the |my |our )?(.+)$", body, re.I)
    if m:
        body, loc = m[1], m[2]
    qty, name = take_quantity(body)
    if qty == 1 and re.match(r"^(?:a|an|one)\s", body, re.I) is None:
        qty = None  # 'some'/'the' shouldn't mean exactly one
    name = strip_articles(name)
    res = search(c, name)
    if loc:
        res = [r for r in res if score(tokens(loc), r["location"]) >= 45] or res
    if not res:
        return {"action": "remove", "speech": f"I couldn't find {name} in your inventory.", "results": []}
    it = res[0]
    if qty and it["quantity"] > qty:
        saved = update_item(c, it["id"], {"quantity": it["quantity"] - qty})
        return {
            "action": "remove",
            "speech": f"Removed {qty}. {saved['quantity']} {saved['name']} left in {spoken_location(saved['location']) or 'the inventory'}.",
            "results": [saved],
            "term": saved["name"],
        }
    delete_item(c, it["id"])
    return {"action": "remove", "speech": f"Removed {it['name']} from your inventory.", "results": [], "removed": it}


CSV_COLUMNS = [
    ("Name", "name"), ("Quantity", "quantity"), ("Location", "location"), ("Stored", "stored"),
    ("Category", "category"), ("Price", "price"), ("Purchase date", "purchase_date"),
    ("Purchased from", "purchased_from"), ("Brand", "brand"), ("Serial", "serial"),
    ("Warranty until", "warranty_until"), ("Notes", "notes"),
]


def export_csv(c):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([h for h, _ in CSV_COLUMNS])
    def cell(i, k):
        if k == "stored":
            return "yes" if i[k] else ""
        return "" if i[k] is None else i[k]

    for i in all_items(c):
        w.writerow([cell(i, k) for _, k in CSV_COLUMNS])
    return buf.getvalue()


# ------------------------------------------------------------------------ http

IMG_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeInventory/1.1"
    raw = b""

    def log_message(self, fmt, *args):
        if os.environ.get("DEBUG"):
            super().log_message(fmt, *args)

    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if not isinstance(body, bytes):
            body = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in {"Cache-Control": "no-store", **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        if not self.raw:
            return {}
        try:
            data = json.loads(self.raw)
            return data if isinstance(data, (dict, list)) else {"text": str(data)}
        except ValueError:
            return {"text": self.raw.decode("utf-8", "replace")}

    def do_OPTIONS(self):
        self.send(204, b"", extra={
            "Access-Control-Allow-Methods": "GET, POST, PATCH, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PATCH(self):
        self.route("PATCH")

    def do_PUT(self):
        self.route("PATCH")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        url = urlparse(self.path)
        path = re.sub(r"/+", "/", url.path).rstrip("/") or "/"
        qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n > MAX_BODY:
                self.close_connection = True
                return self.send(413, {"error": "That file is too large (16 MB max)."})
            self.raw = self.rfile.read(n) if n else b""
            if method == "GET" and path.startswith("/photos/"):
                return self.photo(path[len("/photos/"):])
            if method == "GET" and not path.startswith("/api"):
                return self.static(path)
            with LOCK, db() as c:
                result = self.api(c, method, path, qs)
            if isinstance(result, tuple):
                return self.send(*result)
            return self.send(200, result)
        except ValueError as e:
            return self.send(400, {"error": str(e), "speech": str(e)})
        except LookupError as e:
            return self.send(404, {"error": str(e), "speech": str(e)})
        except Exception as e:  # pragma: no cover
            return self.send(500, {"error": f"Server error: {e}", "speech": "Sorry, the inventory had an error."})

    def api(self, c, method, path, qs):
        m = re.match(r"^/api/items/(\d+)$", path)
        mp = re.match(r"^/api/items/(\d+)/photo$", path)
        if path == "/api/items" and method == "GET":
            return search(c, qs["q"]) if qs.get("q") else all_items(c)
        if path == "/api/items" and method == "POST":
            data = self.body()
            if isinstance(data, list):
                return [add_item(c, d) for d in data]
            return add_item(c, data)
        if mp:
            item_id = int(mp[1])
            if not get_item(c, item_id):
                raise LookupError("Item not found")
            if method in ("POST", "PATCH"):
                return set_photo(c, item_id, self.raw, thumb=truthy(qs.get("thumb")))
            if method == "DELETE":
                return clear_photo(c, item_id)
        if m:
            item_id = int(m[1])
            if not get_item(c, item_id):
                raise LookupError("Item not found")
            if method == "GET":
                return get_item(c, item_id)
            if method == "PATCH":
                return update_item(c, item_id, self.body())
            if method == "DELETE":
                delete_item(c, item_id)
                return {"ok": True}
        if path == "/api/ask":
            d = self.body() if method == "POST" else qs
            d = d if isinstance(d, dict) else {}
            return ask(c, text=d.get("text") or d.get("q"), item=d.get("item"), location=d.get("location"), about=d.get("about"))
        if path == "/api/command" and method == "POST":
            d = self.body()
            return command(c, d.get("text", ""), d.get("action") or None, dry_run=truthy(d.get("dry_run")))
        if path == "/api/import" and method == "POST":
            d = self.body()
            items = d.get("items") if isinstance(d.get("items"), list) else parse_import(d.get("text", ""))
            if truthy(qs.get("dry_run")) or d.get("dry_run"):
                return {"items": canon_batch(c, items), "count": len(items)}
            added = [add_item(c, i) for i in items]
            return {"items": added, "count": len(added), "speech": f"Added {len(added)} item{'s' if len(added) != 1 else ''}."}
        if path == "/api/stats":
            return stats(c)
        if path == "/api/locations" and method == "GET":
            return sorted({l for (l,) in c.execute("SELECT location FROM items WHERE location<>''")}, key=str.lower)
        if path == "/api/locations/rename" and method == "POST":
            d = self.body()
            return rename_location(c, d.get("from", ""), d.get("to", ""))
        if path == "/api/export.csv":
            return (200, export_csv(c), "text/csv; charset=utf-8",
                    {"Content-Disposition": 'attachment; filename="home-inventory.csv"'})
        raise LookupError("Not found")

    def photo(self, name):
        f = PHOTO_DIR / name
        if not re.fullmatch(r"[\w.\-]+", name) or f.parent.resolve() != PHOTO_DIR.resolve() or not f.is_file():
            return self.send(404, {"error": "Photo not found"})
        self.send(200, f.read_bytes(), IMG_TYPES.get(f.suffix.lower(), "application/octet-stream"),
                  {"Cache-Control": "public, max-age=31536000, immutable"})

    def static(self, path):
        name = "index.html" if path == "/" else path.lstrip("/")
        f = (STATIC / name).resolve()
        if not str(f).startswith(str(STATIC.resolve())) or not f.is_file():
            f = STATIC / "index.html"
        types = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                 ".svg": "image/svg+xml", ".json": "application/json", ".png": "image/png"}
        self.send(200, f.read_bytes(), types.get(f.suffix, "application/octet-stream"))


def main():
    init_db()
    print(f"Home Inventory running on http://0.0.0.0:{PORT}  (data: {DATA_DIR})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
