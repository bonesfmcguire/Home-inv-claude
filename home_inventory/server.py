#!/usr/bin/env python3
"""Home Inventory - a tiny, dependency-free home inventory server.

Runs on the Python standard library only (SQLite + http.server), so it works
as a Home Assistant add-on, a Docker container, or just `python3 server.py`.
"""
import csv
import difflib
import io
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
PORT = int(os.environ.get("PORT", "8099"))
STATIC = Path(__file__).parent / "static"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "inventory.db"
LOCK = threading.Lock()

FIELDS = ("name", "quantity", "location", "stored", "category", "notes")

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
        else:
            out[f] = re.sub(r"\s+", " ", str(v or "")).strip()
    if not partial:
        if not out.get("name"):
            raise ValueError("Item needs a name")
        out.setdefault("quantity", 1)
        for f in ("location", "category", "notes"):
            out.setdefault(f, "")
        out.setdefault("stored", 0)
    elif "name" in out and not out["name"]:
        raise ValueError("Item needs a name")
    return out


def all_items(c):
    return [row(r) for r in c.execute("SELECT * FROM items ORDER BY location COLLATE NOCASE, name COLLATE NOCASE")]


def get_item(c, item_id):
    r = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return row(r) if r else None


def add_item(c, data):
    """Add an item, merging quantity into an identical name+location entry."""
    d = clean(data)
    ex = c.execute(
        "SELECT * FROM items WHERE lower(name)=lower(?) AND lower(location)=lower(?)",
        (d["name"], d["location"]),
    ).fetchone()
    if ex:
        upd = {"quantity": ex["quantity"] + d["quantity"], "updated_at": now()}
        if d["stored"]:
            upd["stored"] = 1
        for f in ("category", "notes"):
            if d[f] and not ex[f]:
                upd[f] = d[f]
        sets = ", ".join(f"{k}=?" for k in upd)
        c.execute(f"UPDATE items SET {sets} WHERE id=?", (*upd.values(), ex["id"]))
        return get_item(c, ex["id"])
    ts = now()
    cur = c.execute(
        "INSERT INTO items(name,quantity,location,stored,category,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (d["name"], d["quantity"], d["location"], d["stored"], d["category"], d["notes"], ts, ts),
    )
    return get_item(c, cur.lastrowid)


def update_item(c, item_id, data):
    d = clean(data, partial=True)
    if not d:
        return get_item(c, item_id)
    d["updated_at"] = now()
    sets = ", ".join(f"{k}=?" for k in d)
    c.execute(f"UPDATE items SET {sets} WHERE id=?", (*d.values(), item_id))
    return get_item(c, item_id)


def stats(c):
    r = c.execute(
        "SELECT count(*) n, coalesce(sum(quantity),0) q, count(DISTINCT lower(location)) l, coalesce(sum(stored),0) s FROM items"
    ).fetchone()
    return {"items": r["n"], "quantity": r["q"], "locations": r["l"], "stored": r["s"]}


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


def parse_phrase(text, default_location=""):
    """Turn '3 AA batteries in the kitchen drawer' into an item dict."""
    t = re.sub(r"\s+", " ", str(text)).strip().strip(".!?,;")
    stored = bool(re.search(r"\b(stored|storage|packed away)\b", t, re.I))
    t = re.sub(r"[,;]?\s*\b(?:is |are )?(?:stored|packed away)\b[,;]?\s*", " ", t, flags=re.I).strip(" ,;")
    parts = LOC_SPLIT.split(t, maxsplit=1)
    name, location = parts[0], (parts[1] if len(parts) > 1 else default_location)
    qty, name = take_quantity(name)
    name = strip_articles(name).strip(" ,;")
    return {
        "name": cap(name),
        "quantity": qty or 1,
        "location": cap(strip_articles(location or "").strip(" ,;")),
        "stored": stored,
    }


HEADER_ALIASES = {
    "name": {"name", "item", "items", "thing", "what", "description", "item name", "object"},
    "quantity": {"quantity", "qty", "count", "amount", "#", "number", "how many", "units"},
    "location": {"location", "where", "place", "room", "spot", "area", "loc", "storage location", "bin", "box"},
    "stored": {"stored", "in storage", "storage", "packed", "is stored", "stored?"},
    "category": {"category", "type", "kind", "tag", "tags", "group"},
    "notes": {"notes", "note", "comments", "comment", "details", "info"},
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


def parse_import(text):
    """Accepts pasted spreadsheet cells (TSV), CSV with a header, or a plain list.

    Lists may use 'Location:' heading lines to set the location for the lines below.
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

    items = []
    if delim:
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
        mapping = map_header(rows[0])
        if mapping:
            rows = rows[1:]
        else:
            mapping = dict(enumerate(DEFAULT_COLUMNS))
        for r in rows:
            d = {f: r[i] for i, f in mapping.items() if i < len(r)}
            if not str(d.get("name", "")).strip():
                continue
            d["quantity"] = to_int(d.get("quantity"), 1) if str(d.get("quantity", "")).strip() else 1
            d["stored"] = truthy(d.get("stored"))
            items.append(clean(d))
        return [dict(i, stored=bool(i["stored"])) for i in items]

    current_loc = ""
    for line in lines:
        s = re.sub(r"^\s*(?:[-*•·▪◦]|\d+[.)]|\[[ xX]?\])\s*", "", line).strip()
        if not s:
            continue
        if s.endswith(":") or re.match(r"^#+\s", s):
            current_loc = cap(strip_articles(s.strip("#: ")))
            continue
        it = parse_phrase(s, current_loc)
        if it["name"]:
            items.append(it)
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
                s, name_hit = 0.69 * score(toks, f"{it['category']} {it['notes']}"), False
        if s >= 45:
            out.append((s, it, name_hit))
    if any(hit for _, _, hit in out):
        out = [o for o in out if o[2]]  # drop category/notes-only hits when names match
    out = [(s, it) for s, it, _ in out]
    out.sort(key=lambda x: (-x[0], x[1]["name"].lower()))
    return [it for _, it in out]


def spoken_location(loc):
    """'Kitchen / Junk drawer' -> 'the Junk drawer in the Kitchen'."""
    parts = [p.strip() for p in re.split(r"\s*[/>»]\s*", loc) if p.strip()]
    return " in ".join(f"the {p}" for p in reversed(parts))


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
        parts = [p for p in re.split(r"\s*[/>»]\s*", i["location"]) if p and score(tokens(term), p) < 45]
        return describe(dict(i, location=" / ".join(parts))) if parts else describe(i, with_loc=False)

    names = [rel(i) for i in res]
    return {"speech": f"In {label} you have {join_list(names, 8)}.", "results": res, "term": term, "kind": "location"}


def ask(c, text=None, item=None, location=None):
    if location:
        return answer_location(c, location)
    if item:
        return answer_item(c, strip_articles(item))
    t = re.sub(r"\s+", " ", (text or "")).strip().strip("?.! ").lower()
    t = re.sub(r"\b(what|where)'?s\b", r"\1 is", t)
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
    "ask": r"^(?:where|what|how many|do (?:i|we) have|have i got|find|locate|search|look up|is there|are there|list|show)\b",
    "move": r"^(?:i\s+)?(?:move|moved)\s+",
    "remove": r"^(?:i\s+)?(?:remove|removed|delete|deleted|toss|tossed|throw (?:away|out)|threw (?:away|out)|used up|use up|used|got rid of|get rid of|sold|gave away|donated|lost)\s+",
    "add": r"^(?:i\s+)?(?:add|added|put|store|stored|place|placed|keep|bought|got|have)\s+",
}


def command(c, text, action=None):
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if not t:
        raise ValueError("Say or type something first")
    t = re.sub(r"\b(what|where|who)'?s\b", r"\1 is", t, flags=re.I)
    low = t.lower()
    if not action:
        for a in ("ask", "move", "remove", "add"):
            if re.match(VERBS[a], low):
                action = a
                break
        else:
            action = "ask" if t.endswith("?") or not LOC_SPLIT.search(t) else "add"
    if action == "ask":
        return dict(ask(c, text=t), action="ask")

    stored_verb = bool(re.match(r"^(?:i\s+)?stored?\b", low))
    body = re.sub(VERBS.get(action, "^$"), "", t, flags=re.I).strip()
    body = re.sub(r"\s+(?:to|from|in) (?:the |my )?inventory$", "", body, flags=re.I)

    if action == "add":
        body = re.sub(r"\s+(?:to|into|onto)\s+", " in ", body, count=1, flags=re.I)
        it = parse_phrase(body)
        it["stored"] = it["stored"] or stored_verb
        saved = add_item(c, it)
        return {"action": "add", "speech": f"Added {describe(it)}.", "results": [saved], "term": saved["name"]}

    if action == "move":
        m = re.match(r"^(.+?)(?:\s+from\s+.+?)?\s+(?:to|into|onto|in|inside)\s+(?:the |my |our )?(.+)$", body, re.I)
        if not m:
            raise ValueError("Try: move the tent to the garage")
        name, dest = strip_articles(m[1]), cap(strip_articles(m[2]))
        res = search(c, name)
        if not res:
            return {"action": "move", "speech": f"I couldn't find {name} in your inventory.", "results": []}
        saved = update_item(c, res[0]["id"], {"location": dest})
        return {"action": "move", "speech": f"Moved {saved['name']} to {spoken_location(dest)}.", "results": [saved], "term": saved["name"]}

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
    c.execute("DELETE FROM items WHERE id=?", (it["id"],))
    return {"action": "remove", "speech": f"Removed {it['name']} from your inventory.", "results": [], "removed": it}


def export_csv(c):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Quantity", "Location", "Stored", "Category", "Notes"])
    for i in all_items(c):
        w.writerow([i["name"], i["quantity"], i["location"], "yes" if i["stored"] else "", i["category"], i["notes"]])
    return buf.getvalue()


# ------------------------------------------------------------------------ http


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeInventory/1.0"

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
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, (dict, list)) else {"text": str(data)}
        except ValueError:
            return {"text": raw.decode("utf-8", "replace")}

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
        if path == "/api/items" and method == "GET":
            return search(c, qs["q"]) if qs.get("q") else all_items(c)
        if path == "/api/items" and method == "POST":
            data = self.body()
            if isinstance(data, list):
                return [add_item(c, d) for d in data]
            return add_item(c, data)
        if m:
            item_id = int(m[1])
            if not get_item(c, item_id):
                raise LookupError("Item not found")
            if method == "GET":
                return get_item(c, item_id)
            if method == "PATCH":
                return update_item(c, item_id, self.body())
            if method == "DELETE":
                c.execute("DELETE FROM items WHERE id=?", (item_id,))
                return {"ok": True}
        if path == "/api/ask":
            d = self.body() if method == "POST" else qs
            d = d if isinstance(d, dict) else {}
            return ask(c, text=d.get("text") or d.get("q"), item=d.get("item"), location=d.get("location"))
        if path == "/api/command" and method == "POST":
            d = self.body()
            return command(c, d.get("text", ""), d.get("action") or None)
        if path == "/api/import" and method == "POST":
            d = self.body()
            items = d.get("items") if isinstance(d.get("items"), list) else parse_import(d.get("text", ""))
            if truthy(qs.get("dry_run")) or d.get("dry_run"):
                return {"items": items, "count": len(items)}
            added = [add_item(c, i) for i in items]
            return {"items": added, "count": len(added), "speech": f"Imported {len(added)} items."}
        if path == "/api/stats":
            return stats(c)
        if path == "/api/locations":
            return [r[0] for r in c.execute("SELECT DISTINCT location FROM items WHERE location<>'' ORDER BY location COLLATE NOCASE")]
        if path == "/api/export.csv":
            return (200, export_csv(c), "text/csv; charset=utf-8",
                    {"Content-Disposition": 'attachment; filename="home-inventory.csv"'})
        raise LookupError("Not found")

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
    print(f"Home Inventory running on http://0.0.0.0:{PORT}  (data: {DB_PATH})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
