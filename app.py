"""Slate: a phone scoreboard for a table of people. No account."""

from __future__ import annotations

import hashlib
import html
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
DB_PATH = DATA / "slate.db"
BIND_HOST = os.environ.get("SLATE_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("SLATE_PORT", "8882"))
TTL_SECONDS = 30 * 24 * 3600
MAX_PLAYERS = 12
MAX_ROUNDS = 200
RATE_LIMIT = 8
RATE_WINDOW = 3600
MARKS = ("#e2b657", "#8eb6ff", "#f08a78", "#7dcea0", "#d7a1e4", "#f0c14b", "#7ec8c3", "#e7e1d5")

_hits: dict[str, list[float]] = {}
_page_cache: dict[str, tuple[float, str]] = {}


class SlateError(ValueError):
    pass


def _now() -> int:
    return int(time.time())


def _client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()[:64]
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    if request.client and request.client.host:
        return request.client.host
    return "local"


def _rate_ok(ip: str) -> bool:
    now = time.time()
    window = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
    if len(window) >= RATE_LIMIT:
        _hits[ip] = window
        return False
    window.append(now)
    _hits[ip] = window
    return True


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _clean_name(value: str, limit: int, label: str) -> str:
    text = " ".join((value or "").split())
    if not text:
        raise SlateError(f"Need a {label}.")
    if len(text) > limit:
        raise SlateError(f"{label.capitalize()} is too long.")
    return text


def _connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS slates (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              owner_hash TEXT NOT NULL,
              low_wins INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL,
              expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS players (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slate_id TEXT NOT NULL,
              name TEXT NOT NULL,
              position INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rounds (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slate_id TEXT NOT NULL,
              label TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scores (
              round_id INTEGER NOT NULL,
              player_id INTEGER NOT NULL,
              points INTEGER NOT NULL,
              PRIMARY KEY (round_id, player_id)
            );
            """
        )


def rank(players: list[dict[str, Any]], low_wins: bool) -> list[dict[str, Any]]:
    rows = [dict(p) for p in players]
    if not rows:
        return rows
    totals = [int(p["total"]) for p in rows]
    if not any(p.get("played") for p in rows):
        for row in rows:
            row["lead"] = False
            row["bar"] = 0
        return rows
    best = min(totals) if low_wins else max(totals)
    span = max(abs(n) for n in totals) or 1
    for row in rows:
        row["lead"] = int(row["total"]) == best
        row["bar"] = 0 if any(n < 0 for n in totals) else int(round(100 * max(int(row["total"]), 0) / span))
    rows.sort(key=lambda p: (0 if p["lead"] else 1, int(p["total"]) if low_wins else -int(p["total"]), p["position"]))
    return rows


def _load(conn: sqlite3.Connection, slate_id: str) -> dict[str, Any] | None:
    slate = conn.execute("SELECT * FROM slates WHERE id = ?", (slate_id,)).fetchone()
    if slate is None:
        return None
    if int(slate["expires_at"]) <= _now():
        return None
    players = conn.execute(
        "SELECT id, name, position FROM players WHERE slate_id = ? ORDER BY position",
        (slate_id,),
    ).fetchall()
    totals = {int(p["id"]): 0 for p in players}
    played = {int(p["id"]): False for p in players}
    score_rows = conn.execute(
        """
        SELECT s.player_id, s.points
        FROM scores s
        JOIN rounds r ON r.id = s.round_id
        WHERE r.slate_id = ?
        """,
        (slate_id,),
    ).fetchall()
    for row in score_rows:
        pid = int(row["player_id"])
        totals[pid] = totals.get(pid, 0) + int(row["points"])
        played[pid] = True
    packed = [
        {
            "id": int(p["id"]),
            "name": p["name"],
            "position": int(p["position"]),
            "total": totals[int(p["id"])],
            "played": played[int(p["id"])],
        }
        for p in players
    ]
    rounds = conn.execute(
        "SELECT id, label, created_at FROM rounds WHERE slate_id = ? ORDER BY id DESC LIMIT 12",
        (slate_id,),
    ).fetchall()
    round_views = []
    for rnd in rounds:
        bits = conn.execute(
            """
            SELECT p.name, p.id AS player_id, s.points
            FROM scores s
            JOIN players p ON p.id = s.player_id
            WHERE s.round_id = ?
            ORDER BY p.position
            """,
            (int(rnd["id"]),),
        ).fetchall()
        round_views.append(
            {
                "id": int(rnd["id"]),
                "label": rnd["label"],
                "created_at": int(rnd["created_at"]),
                "scores": [
                    {"name": b["name"], "player_id": int(b["player_id"]), "points": int(b["points"])}
                    for b in bits
                ],
            }
        )
    return {
        "id": slate["id"],
        "name": slate["name"],
        "low_wins": bool(slate["low_wins"]),
        "expires_at": int(slate["expires_at"]),
        "players": rank(packed, bool(slate["low_wins"])),
        "rounds": round_views,
        "round_count": conn.execute("SELECT COUNT(*) FROM rounds WHERE slate_id = ?", (slate_id,)).fetchone()[0],
    }


def create_slate(name: str, player_names: list[str], low_wins: bool) -> tuple[str, str]:
    title = _clean_name(name, 40, "slate name")
    names: list[str] = []
    seen: set[str] = set()
    for raw in player_names:
        text = " ".join((raw or "").split())
        if not text:
            continue
        if len(text) > 24:
            raise SlateError("Player names are limited to 24 characters.")
        key = text.casefold()
        if key in seen:
            raise SlateError("Player names have to be different.")
        seen.add(key)
        names.append(text)
    if len(names) < 2:
        raise SlateError("Need at least two players.")
    if len(names) > MAX_PLAYERS:
        raise SlateError("12 players max.")
    key = secrets.token_urlsafe(18)
    owner_hash = _hash_key(key)
    now = _now()
    with _connect() as conn:
        for _ in range(5):
            sid = secrets.token_urlsafe(6)
            if conn.execute("SELECT 1 FROM slates WHERE id = ?", (sid,)).fetchone():
                continue
            conn.execute(
                "INSERT INTO slates (id, name, owner_hash, low_wins, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (sid, title, owner_hash, 1 if low_wins else 0, now, now + TTL_SECONDS),
            )
            for i, player in enumerate(names):
                conn.execute(
                    "INSERT INTO players (slate_id, name, position) VALUES (?, ?, ?)",
                    (sid, player, i),
                )
            return sid, key
    raise SlateError("Could not open a slate. Try again.")


def add_round(slate_id: str, key: str, label: str, points: dict[int, int]) -> None:
    _require_owner(slate_id, key)
    text = " ".join((label or "").split())
    if len(text) > 40:
        raise SlateError("Round name is too long.")
    with _connect() as conn:
        slate = _load(conn, slate_id)
        if slate is None:
            raise SlateError("That slate is gone.")
        if int(slate["round_count"]) >= MAX_ROUNDS:
            raise SlateError("This slate is full. 200 rounds max.")
        ids = {int(p["id"]) for p in slate["players"]}
        if set(points) != ids:
            raise SlateError("Need one score for every player.")
        for value in points.values():
            if value < -999 or value > 9999:
                raise SlateError("Scores have to be whole numbers from -999 to 9999.")
        cur = conn.execute(
            "INSERT INTO rounds (slate_id, label, created_at) VALUES (?, ?, ?)",
            (slate_id, text, _now()),
        )
        rid = int(cur.lastrowid)
        for pid, value in points.items():
            conn.execute(
                "INSERT INTO scores (round_id, player_id, points) VALUES (?, ?, ?)",
                (rid, pid, value),
            )


def undo_round(slate_id: str, key: str) -> None:
    _require_owner(slate_id, key)
    with _connect() as conn:
        slate = _load(conn, slate_id)
        if slate is None or not slate["rounds"]:
            raise SlateError("Nothing to undo.")
        rid = int(slate["rounds"][0]["id"])
        conn.execute("DELETE FROM scores WHERE round_id = ?", (rid,))
        conn.execute("DELETE FROM rounds WHERE id = ?", (rid,))


def save_last(slate_id: str, key: str, label: str, points: dict[int, int]) -> None:
    _require_owner(slate_id, key)
    text = " ".join((label or "").split())
    if len(text) > 40:
        raise SlateError("Round name is too long.")
    with _connect() as conn:
        slate = _load(conn, slate_id)
        if slate is None:
            raise SlateError("That slate is gone.")
        if not slate["rounds"]:
            raise SlateError("No round to fix.")
        ids = {int(p["id"]) for p in slate["players"]}
        if set(points) != ids:
            raise SlateError("Need one score for every player.")
        for value in points.values():
            if value < -999 or value > 9999:
                raise SlateError("Scores have to be whole numbers from -999 to 9999.")
        rid = int(slate["rounds"][0]["id"])
        conn.execute("UPDATE rounds SET label = ? WHERE id = ?", (text, rid))
        for pid, value in points.items():
            conn.execute(
                "UPDATE scores SET points = ? WHERE round_id = ? AND player_id = ?",
                (value, rid, pid),
            )


def rename_players(slate_id: str, key: str, names: dict[int, str]) -> None:
    _require_owner(slate_id, key)
    cleaned: dict[int, str] = {}
    seen: set[str] = set()
    for pid, raw in names.items():
        text = _clean_name(raw, 24, "player name")
        fold = text.casefold()
        if fold in seen:
            raise SlateError("Player names have to be different.")
        seen.add(fold)
        cleaned[pid] = text
    with _connect() as conn:
        slate = _load(conn, slate_id)
        if slate is None:
            raise SlateError("That slate is gone.")
        ids = {int(p["id"]) for p in slate["players"]}
        if set(cleaned) != ids:
            raise SlateError("Need every player.")
        for pid, text in cleaned.items():
            conn.execute(
                "UPDATE players SET name = ? WHERE id = ? AND slate_id = ?",
                (text, pid, slate_id),
            )


def add_player(slate_id: str, key: str, name: str) -> None:
    _require_owner(slate_id, key)
    text = _clean_name(name, 24, "player name")
    with _connect() as conn:
        slate = _load(conn, slate_id)
        if slate is None:
            raise SlateError("That slate is gone.")
        if len(slate["players"]) >= MAX_PLAYERS:
            raise SlateError("12 players max.")
        if any(p["name"].casefold() == text.casefold() for p in slate["players"]):
            raise SlateError("Player names have to be different.")
        position = max(int(p["position"]) for p in slate["players"]) + 1
        cur = conn.execute(
            "INSERT INTO players (slate_id, name, position) VALUES (?, ?, ?)",
            (slate_id, text, position),
        )
        pid = int(cur.lastrowid)
        rounds = conn.execute("SELECT id FROM rounds WHERE slate_id = ?", (slate_id,)).fetchall()
        for rnd in rounds:
            conn.execute(
                "INSERT INTO scores (round_id, player_id, points) VALUES (?, ?, 0)",
                (int(rnd["id"]), pid),
            )


def _require_owner(slate_id: str, key: str) -> None:
    if not key or len(key) > 80:
        raise SlateError("That owner link is not valid.")
    with _connect() as conn:
        row = conn.execute("SELECT owner_hash, expires_at FROM slates WHERE id = ?", (slate_id,)).fetchone()
        if row is None or int(row["expires_at"]) <= _now():
            raise SlateError("That slate is gone.")
        if not secrets.compare_digest(row["owner_hash"], _hash_key(key)):
            raise SlateError("That owner link is not valid.")


def _asset_version(*paths: Path) -> str:
    m = 0.0
    for p in paths:
        try:
            m = max(m, p.stat().st_mtime)
        except OSError:
            pass
    return str(int(m))


def _shell(name: str) -> str:
    html_path = STATIC / name
    css_path = STATIC / "style.css"
    js_path = STATIC / "home.js"
    stamp = max(
        html_path.stat().st_mtime,
        css_path.stat().st_mtime if css_path.exists() else 0.0,
        js_path.stat().st_mtime if js_path.exists() else 0.0,
    )
    cached = _page_cache.get(name)
    if cached and cached[0] == stamp:
        return cached[1]
    page = html_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    page = re.sub(
        r"""[ \t]*<link[^>]+href=["']/static/style\.css[^"']*["'][^>]*>\s*""",
        "",
        page,
        flags=re.I,
    )
    page = re.sub(r"</head>", f'<style id="slate-css">\n{css}\n</style>\n</head>', page, count=1, flags=re.I)
    v = _asset_version(js_path, css_path)
    page = re.sub(
        r"""src=(["'])/static/home\.js(?:\?[^"']*)?\1""",
        rf"src=\1/static/home.js?v={v}\1",
        page,
    )
    _page_cache[name] = (stamp, page)
    return page


def _player_inputs(names: list[str]) -> str:
    slots = names[:MAX_PLAYERS] if names else ["", ""]
    while len(slots) < 4:
        slots.append("")
    bits = []
    for i, name in enumerate(slots):
        req = " required" if i < 2 else ""
        bits.append(
            '<label class="field"><span>Player</span>'
            f'<input name="player" type="text" maxlength="24" autocomplete="off"{req} value="{html.escape(name, quote=True)}" />'
            "</label>"
        )
    return "".join(bits)


def render_home(status: str = "", status_class: str = "", name: str = "", names: list[str] | None = None, low: bool = False) -> str:
    page = _shell("index.html")
    page = page.replace("<!--NAME-->", html.escape(name, quote=True))
    page = page.replace("<!--LOW-->", "checked" if low else "")
    page = page.replace("<!--PLAYERS-->", _player_inputs(names or []))
    page = page.replace("<!--STATUS-->", html.escape(status))
    page = page.replace("<!--STATUS_CLASS-->", status_class)
    return page


def _when(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y")


def _leader_html(slate: dict[str, Any]) -> str:
    leads = [p for p in slate["players"] if p.get("lead")]
    if not leads:
        mode = "Lowest score leads." if slate["low_wins"] else "Highest score leads."
        return (
            '<div class="leader"><p class="who">Waiting on the first round.</p>'
            f'<p class="sub">{html.escape(mode)} Open until {_when(slate["expires_at"])}.</p></div>'
        )
    names = ", ".join(p["name"] for p in leads)
    title = "Tied" if len(leads) > 1 else "Leading"
    gap = ""
    if len(leads) == 1 and len(slate["players"]) > 1:
        nxt = next(p for p in slate["players"] if not p.get("lead"))
        gap = f' Ahead by {abs(int(leads[0]["total"]) - int(nxt["total"]))}.'
    elif len(leads) > 1:
        gap = " Tied."
    return (
        '<div class="leader">'
        f'<p class="eyebrow">{html.escape(title)}</p>'
        f'<p class="who">{html.escape(names)}</p>'
        f'<p class="pts">{leads[0]["total"]}</p>'
        f'<p class="sub">{gap.strip()} Open until {_when(slate["expires_at"])}.</p>'
        "</div>"
    )


def _rows_html(slate: dict[str, Any], *, table: bool = False) -> str:
    bits = []
    for place, p in enumerate(slate["players"], start=1):
        mark = MARKS[int(p["position"]) % len(MARKS)]
        lead = " lead" if p.get("lead") else ""
        bar = ""
        if p.get("played") and not any(int(x["total"]) < 0 for x in slate["players"]):
            bar = f'<div class="bar"><span style="width:{int(p["bar"])}%"></span></div>'
        label = "leader" if p.get("lead") else f"place {place}"
        spark = _sparkline_html(slate, int(p["id"])) if table else ""
        bits.append(
            f'<div class="row{lead}" style="--mark:{mark}">'
            f'<div><strong>{html.escape(p["name"])}</strong><em>{html.escape(label)}</em>{bar}{spark}</div>'
            f'<div class="pts">{int(p["total"])}</div>'
            "</div>"
        )
    return f'<div class="rows">{"".join(bits)}</div>'


def _round_totals(slate: dict[str, Any]) -> dict[int, list[int]]:
    history = {int(p["id"]): [] for p in slate["players"]}
    totals = {int(p["id"]): 0 for p in slate["players"]}
    for rnd in reversed(slate["rounds"]):
        by_id = {int(s["player_id"]): int(s["points"]) for s in rnd["scores"]}
        for pid in history:
            totals[pid] += by_id.get(pid, 0)
            history[pid].append(totals[pid])
    return history


def _sparkline_html(slate: dict[str, Any], player_id: int) -> str:
    vals = _round_totals(slate).get(player_id, [])[-8:]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1
    dots = []
    for value in vals:
        pct = 100 - int(round(((value - lo) / span) * 100))
        dots.append(f'<i style="--y:{pct}%"></i>')
    return f'<div class="spark" aria-hidden="true">{"".join(dots)}</div>'


def _last_round_html(slate: dict[str, Any]) -> str:
    if not slate["rounds"]:
        return '<p class="hint">No rounds yet. The table updates when the owner adds one.</p>'
    rnd = slate["rounds"][0]
    title = rnd["label"] or "Last round"
    ordered = sorted(rnd["scores"], key=lambda s: int(s["points"]), reverse=not slate["low_wins"])
    chips = []
    for item in ordered:
        pts = int(item["points"])
        sign = "+" if pts > 0 else ""
        chips.append(f'<span class="chip"><b>{html.escape(item["name"])}</b> {sign}{pts}</span>')
    return f'<h2>{html.escape(title)}</h2><div class="chips">{"".join(chips)}</div>'


def _table_stats_html(slate: dict[str, Any]) -> str:
    mode = "Low wins" if slate["low_wins"] else "High wins"
    return (
        '<section class="table-stats" aria-label="Slate stats">'
        f'<div><b>{int(slate["round_count"])}</b><span>rounds</span></div>'
        f'<div><b>{len(slate["players"])}</b><span>players</span></div>'
        f'<div><b>{html.escape(mode)}</b><span>mode</span></div>'
        '</section>'
    )


def _table_panel_html(slate: dict[str, Any]) -> str:
    return (
        '<section class="table-panel">'
        + _leader_html(slate)
        + _table_stats_html(slate)
        + '<section class="last-card">'
        + _last_round_html(slate)
        + '</section>'
        + '</section>'
    )


def _rounds_html(slate: dict[str, Any]) -> str:
    if not slate["rounds"]:
        return '<p class="hint">No rounds yet.</p>'
    bits = []
    for rnd in slate["rounds"]:
        label = rnd["label"] or "Round"
        scores = ", ".join(f'{s["name"]} {s["points"]}' for s in rnd["scores"])
        bits.append(f'<div class="round"><b>{html.escape(label)}</b><span>{html.escape(scores)}</span></div>')
    return "".join(bits)


def _summary_text(slate: dict[str, Any]) -> str:
    lines = [slate["name"]]
    for player in slate["players"]:
        mark = " *" if player.get("lead") else ""
        lines.append(f"{player['name']} {int(player['total'])}{mark}")
    return "\n".join(lines)


def render_board(slate: dict[str, Any], table: bool = False) -> str:
    page = _shell("sheet.html")
    if table:
        main = (
            f'<p class="eyebrow">Live table</p><h1>{html.escape(slate["name"])}</h1>'
            + '<p class="lede">Leave this open. It checks for new rounds and reloads itself.</p>'
            + _table_panel_html(slate)
            + _rows_html(slate, table=True)
            + '<p class="live-note">Auto refreshes when a new round lands.</p>'
            + '<section class="card quiet"><h2>Last rounds</h2>'
            + _rounds_html(slate)
            + "</section>"
            + '<noscript><p class="hint">JavaScript is off. Pull to refresh for new scores.</p></noscript>'
        )
    else:
        main = (
            f'<p class="eyebrow">Board</p><h1>{html.escape(slate["name"])}</h1>'
            + '<p class="lede">Read only. New rounds show up on their own.</p><button type="button" class="btn sm" id="big-type">Big type</button>'
            + _leader_html(slate)
            + _rows_html(slate)
            + '<section class="card quiet"><h2>Text</h2><textarea class="summary" id="summary" readonly>'
            + html.escape(_summary_text(slate))
            + '</textarea><button type="button" class="btn sm" id="copy-summary">Copy text</button></section>'
            + '<section class="card quiet"><h2>Last rounds</h2>'
            + _rounds_html(slate)
            + "</section>"
            + '<noscript><p class="hint">JavaScript is off. Pull to refresh for new scores.</p></noscript>'
        )
    klass = "table big" if table else ""
    attrs = f'class="{klass}" data-id="{html.escape(slate["id"], quote=True)}" data-rounds="{int(slate["round_count"])}"'
    page = page.replace("<!--TITLE-->", html.escape(slate["name"] + " · Slate"))
    page = page.replace("<!--REFRESH-->", '<meta http-equiv="refresh" content="20" />')
    page = page.replace("<!--BODY_ATTR-->", attrs.strip())
    page = page.replace("<!--MAIN-->", main)
    return page


def _board_url(request: Request, slate_id: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/s/{slate_id}"


def render_owner(slate: dict[str, Any], key: str, board_url: str, status: str = "", status_class: str = "") -> str:
    page = _shell("sheet.html")
    action = f"/s/{slate['id']}/{key}/round"
    undo = f"/s/{slate['id']}/{key}/undo"
    board = board_url
    fields = []
    fix_fields = []
    last = slate["rounds"][0] if slate["rounds"] else None
    last_points = {int(s["player_id"]): int(s["points"]) for s in last["scores"]} if last else {}
    for p in sorted(slate["players"], key=lambda item: item["position"]):
        fields.append(
            '<label class="field"><span>'
            + html.escape(p["name"])
            + '</span><input name="score_'
            + str(int(p["id"]))
            + '" type="number" inputmode="numeric" required min="-999" max="9999" placeholder="0" value="" />'
            "</label>"
        )
        if last:
            fix_fields.append(
                '<label class="field"><span>'
                + html.escape(p["name"])
                + '</span><input name="score_'
                + str(int(p["id"]))
                + f'" type="number" inputmode="numeric" required min="-999" max="9999" value="{last_points.get(int(p["id"]), 0)}" />'
                "</label>"
            )
    fix_form = ""
    if last:
        fix_action = f"/s/{slate['id']}/{key}/last"
        fix_form = (
            f'<form class="card" method="post" action="{html.escape(fix_action, quote=True)}">'
            "<h2>Fix last round</h2>"
            + '<label class="field"><span>Round name</span><input name="label" type="text" maxlength="40" autocomplete="off" value="'
            + html.escape(last["label"], quote=True)
            + '" /></label>'
            + "".join(fix_fields)
            + '<button type="submit" class="btn lg">Save last round</button>'
            + "</form>"
        )
    main = (
        f'<p class="eyebrow">Owner</p><h1>{html.escape(slate["name"])}</h1>'
        '<p class="lede">This page is the key. Bookmark it. The board link below is the one you pass around.</p>'
        + _leader_html(slate)
        + _rows_html(slate)
        + '<section class="card"><h2>Board link</h2>'
        + f'<p class="url" id="board-url">{html.escape(board)}</p>'
        + f'<p class="hint"><a href="{html.escape(board + "/table", quote=True)}">Table view</a> is the one to prop up. No edits there.</p>'
        + f'<button type="button" class="btn sm" id="copy-board" data-url="{html.escape(board, quote=True)}">Copy board link</button>'
        + '<p class="hint">That link cannot add scores.</p></section>'
        + f'<form class="card" method="post" action="{html.escape(action, quote=True)}">'
        + "<h2>Add a round</h2>"
        + '<label class="field"><span>Round name</span><input name="label" type="text" maxlength="40" autocomplete="off" placeholder="Hand 3" /></label>'
        + "".join(fields)
        + '<button type="submit" class="btn primary lg">Add round</button>'
        + f'<p class="status{status_class}" role="status">{html.escape(status)}</p>'
        + "</form>"
        + fix_form
        + _player_forms(slate, key)
        + '<section class="card"><h2>Rounds</h2>'
        + _rounds_html(slate)
        + f'<form method="post" action="{html.escape(undo, quote=True)}" style="margin-top:12px">'
        + '<button type="submit" class="btn danger sm">Undo last round</button></form>'
        + "</section>"
    )
    page = page.replace("<!--TITLE-->", html.escape(slate["name"] + " · owner"))
    page = page.replace("<!--REFRESH-->", "")
    page = page.replace("<!--BODY_ATTR-->", "")
    page = page.replace("<!--MAIN-->", main)
    return page


def _player_forms(slate: dict[str, Any], key: str) -> str:
    rename_action = f"/s/{slate['id']}/{key}/rename"
    add_action = f"/s/{slate['id']}/{key}/player"
    names = []
    for player in sorted(slate["players"], key=lambda item: item["position"]):
        names.append(
            '<label class="field"><span>Name</span><input name="name_'
            + str(int(player["id"]))
            + '" type="text" maxlength="24" required autocomplete="off" value="'
            + html.escape(player["name"], quote=True)
            + '" /></label>'
        )
    add = ""
    if len(slate["players"]) < MAX_PLAYERS:
        add = (
            f'<form method="post" action="{html.escape(add_action, quote=True)}">'
            '<label class="field"><span>Late player</span><input name="name" type="text" maxlength="24" required autocomplete="off" placeholder="Name" /></label>'
            '<button type="submit" class="btn sm">Add player</button>'
            '<p class="hint">Past rounds count them as 0.</p></form>'
        )
    return (
        '<details class="card"><summary>Players</summary>'
        + f'<form method="post" action="{html.escape(rename_action, quote=True)}">'
        + "".join(names)
        + '<button type="submit" class="btn sm">Save names</button></form>'
        + add
        + "</details>"
    )


def _gone() -> HTMLResponse:
    page = _shell("sheet.html")
    page = page.replace("<!--TITLE-->", "Slate")
    page = page.replace("<!--REFRESH-->", "")
    page = page.replace("<!--BODY_ATTR-->", "")
    page = page.replace(
        "<!--MAIN-->",
        '<p class="eyebrow">Gone</p><h1>That slate is not here.</h1>'
        '<p class="lede">It expired, or the link is wrong. Owner links that do not match look the same as missing ones.</p>'
        '<a class="btn primary" href="/">Start one</a>',
    )
    return HTMLResponse(page, status_code=404)


app = FastAPI(title="Slate", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.middleware("http")
async def _headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    resp: Response = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'self'"
    )
    if request.url.path == "/" or request.url.path.startswith("/s/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "slate"}


@app.get("/robots.txt")
def robots() -> Response:
    return Response((STATIC / "robots.txt").read_text(encoding="utf-8"), media_type="text/plain")


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return HTMLResponse(render_home())


@app.post("/api/slates")
async def create(request: Request) -> Response:
    if not _rate_ok(_client_ip(request)):
        return HTMLResponse(
            render_home("Too many new slates from this network. Try again in a bit.", " bad"),
            status_code=429,
        )
    form = await request.form()
    name = str(form.get("name") or "")
    players = [str(v) for v in form.getlist("player")]
    low = form.get("low_wins") == "1"
    try:
        sid, key = create_slate(name, players, low)
    except SlateError as err:
        return HTMLResponse(render_home(str(err), " bad", name, players, low), status_code=400)
    return RedirectResponse(f"/s/{sid}/{key}", status_code=303)


def _fetch(slate_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        return _load(conn, slate_id)


@app.get("/s/{slate_id}", response_class=HTMLResponse)
def board(slate_id: str) -> HTMLResponse:
    slate = _fetch(slate_id)
    if slate is None:
        return _gone()
    return HTMLResponse(render_board(slate))


@app.get("/s/{slate_id}/table", response_class=HTMLResponse)
def table(slate_id: str) -> HTMLResponse:
    slate = _fetch(slate_id)
    if slate is None:
        return _gone()
    return HTMLResponse(render_board(slate, table=True))


@app.get("/api/board/{slate_id}")
def board_json(slate_id: str) -> dict[str, Any]:
    slate = _fetch(slate_id)
    if slate is None:
        raise HTTPException(404, "That slate is gone.")
    return {
        "id": slate["id"],
        "name": slate["name"],
        "low_wins": slate["low_wins"],
        "round_count": slate["round_count"],
        "players": [
            {"name": p["name"], "total": int(p["total"]), "lead": bool(p.get("lead"))}
            for p in slate["players"]
        ],
    }


@app.get("/s/{slate_id}/{key}", response_class=HTMLResponse)
def owner(slate_id: str, key: str, request: Request) -> HTMLResponse:
    try:
        _require_owner(slate_id, key)
    except SlateError:
        return _gone()
    slate = _fetch(slate_id)
    if slate is None:
        return _gone()
    return HTMLResponse(render_owner(slate, key, _board_url(request, slate_id)))


def _parse_points(form, players: list[dict[str, Any]]) -> dict[int, int]:
    points: dict[int, int] = {}
    for player in players:
        raw = str(form.get(f"score_{player['id']}") or "").strip()
        if not re.fullmatch(r"-?\d{1,4}", raw):
            raise SlateError("Scores have to be whole numbers from -999 to 9999.")
        points[int(player["id"])] = int(raw)
    return points


@app.post("/s/{slate_id}/{key}/round")
async def post_round(slate_id: str, key: str, request: Request) -> Response:
    try:
        _require_owner(slate_id, key)
    except SlateError:
        return _gone()
    form = await request.form()
    slate = _fetch(slate_id)
    if slate is None:
        return _gone()
    try:
        points = _parse_points(form, slate["players"])
        add_round(slate_id, key, str(form.get("label") or ""), points)
    except SlateError as err:
        slate = _fetch(slate_id)
        if slate is None:
            return _gone()
        return HTMLResponse(render_owner(slate, key, _board_url(request, slate_id), str(err), " bad"), status_code=400)
    return RedirectResponse(f"/s/{slate_id}/{key}", status_code=303)


@app.post("/s/{slate_id}/{key}/last")
async def post_last(slate_id: str, key: str, request: Request) -> Response:
    try:
        _require_owner(slate_id, key)
    except SlateError:
        return _gone()
    form = await request.form()
    slate = _fetch(slate_id)
    if slate is None:
        return _gone()
    try:
        save_last(slate_id, key, str(form.get("label") or ""), _parse_points(form, slate["players"]))
    except SlateError as err:
        if "not valid" in str(err) or "gone" in str(err):
            return _gone()
        slate = _fetch(slate_id)
        if slate is None:
            return _gone()
        return HTMLResponse(render_owner(slate, key, _board_url(request, slate_id), str(err), " bad"), status_code=400)
    return RedirectResponse(f"/s/{slate_id}/{key}", status_code=303)


def _owner_or_gone(slate_id: str, key: str):
    try:
        _require_owner(slate_id, key)
    except SlateError:
        return None
    return _fetch(slate_id)


@app.post("/s/{slate_id}/{key}/rename")
async def post_rename(slate_id: str, key: str, request: Request) -> Response:
    if _owner_or_gone(slate_id, key) is None:
        return _gone()
    form = await request.form()
    slate = _fetch(slate_id)
    if slate is None:
        return _gone()
    names: dict[int, str] = {}
    for player in slate["players"]:
        names[int(player["id"])] = str(form.get(f"name_{player['id']}") or "")
    try:
        rename_players(slate_id, key, names)
    except SlateError as err:
        if "not valid" in str(err) or "gone" in str(err):
            return _gone()
        slate = _fetch(slate_id)
        if slate is None:
            return _gone()
        return HTMLResponse(render_owner(slate, key, _board_url(request, slate_id), str(err), " bad"), status_code=400)
    return RedirectResponse(f"/s/{slate_id}/{key}", status_code=303)


@app.post("/s/{slate_id}/{key}/player")
async def post_player(slate_id: str, key: str, request: Request) -> Response:
    if _owner_or_gone(slate_id, key) is None:
        return _gone()
    form = await request.form()
    try:
        add_player(slate_id, key, str(form.get("name") or ""))
    except SlateError as err:
        if "not valid" in str(err) or "gone" in str(err):
            return _gone()
        slate = _fetch(slate_id)
        if slate is None:
            return _gone()
        return HTMLResponse(render_owner(slate, key, _board_url(request, slate_id), str(err), " bad"), status_code=400)
    return RedirectResponse(f"/s/{slate_id}/{key}", status_code=303)


@app.post("/s/{slate_id}/{key}/undo")
def post_undo(slate_id: str, key: str, request: Request) -> Response:
    try:
        undo_round(slate_id, key)
    except SlateError as err:
        if "not valid" in str(err) or "gone" in str(err):
            return _gone()
        slate = _fetch(slate_id)
        if slate is None:
            return _gone()
        return HTMLResponse(render_owner(slate, key, _board_url(request, slate_id), str(err), " bad"), status_code=400)
    return RedirectResponse(f"/s/{slate_id}/{key}", status_code=303)


def _self_check() -> None:
    players = [
        {"id": 1, "name": "Ada", "position": 0, "total": 10, "played": True},
        {"id": 2, "name": "Bea", "position": 1, "total": 4, "played": True},
        {"id": 3, "name": "Cy", "position": 2, "total": 10, "played": True},
    ]
    ranked = rank(players, False)
    assert [p["name"] for p in ranked[:2]] == ["Ada", "Cy"], ranked
    assert ranked[0]["lead"] and ranked[1]["lead"] and not ranked[2]["lead"]
    low = rank(players, True)
    assert low[0]["name"] == "Bea" and low[0]["lead"]
    empty = rank(
        [{"id": 1, "name": "A", "position": 0, "total": 0, "played": False}],
        False,
    )
    assert empty[0]["lead"] is False
    print("rank ok")


if __name__ == "__main__":
    _self_check()
