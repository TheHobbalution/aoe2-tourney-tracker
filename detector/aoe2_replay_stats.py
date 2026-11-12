#!/usr/bin/env python3
"""
AoE2 DE replay stats - Hobba's free alternative to replay "Pro" stats.

Reads an AoE2 DE recorded game (.aoe2record, or the .zip from aoe.ms) and works out,
for every player:
  - APM and eAPM (eAPM ignores rapid repeat/spam commands)
  - when they clicked Feudal / Castle / Imperial (and when that would finish)
  - opening (Scouts, Archers, M@A, Drush, Fast Castle, Towers...)
  - build order (first buildings, units and techs with times)
  - units made, techs researched, buildings placed
  - estimated resources spent: economy / military / techs / buildings
  - ESTIMATED TC idle time per age (Dark, Feudal, Castle, Imperial)
  - EXPERIMENTAL average floated resources (total of all four), overall and per age
  - resign time, game length, chat

What it can NOT know (replays only store commands, not what happened):
  real gather rates, kills/losses, actual army value, housed time.

Usage:
  python aoe2_replay_stats.py replay.aoe2record
  python aoe2_replay_stats.py replay.zip --player TheHobbalution
  python aoe2_replay_stats.py --download 512876541 311505      (game id, profile id)
  python aoe2_replay_stats.py --latest 311505                   (your newest ranked game)
  add --json stats.json to also save everything as JSON (for the CYD later)

Needs: Python 3.10+, and  pip install mgz-fast
Names/costs come from aoe2_names.json (keep it next to this script).
"""
import argparse
import io
import json
import os
import sys
import urllib.request
import zipfile
from collections import Counter, defaultdict

try:
    from mgz.fast import operation, meta
    from mgz.fast.header import parse as parse_header
    from mgz.fast.enums import Operation, Action
except ImportError:
    sys.exit("Missing the replay reader. Install it with:  pip install mgz-fast")

# mgz-fast reads the current DE "queue" command one field short (it returns 1 or 2 instead of the
# first building id and drops the last id). Re-read those bytes ourselves.
import struct as _struct
import mgz.fast as _mf
_orig_parse_action = _mf.parse_action
def _parse_action_fixed(action_type, data):
    out = _orig_parse_action(action_type, data)
    if action_type == Action.DE_QUEUE:
        try:
            body = data[3:]
            selected, _btype, unit_id, amount = _struct.unpack_from('<h4xhhh', body)
            ids = list(_struct.unpack_from(f'<{selected}I', body, 16)) if selected > 0 else []
            out = dict(out, object_ids=ids, unit_id=unit_id, amount=amount)
        except _struct.error:
            pass
    return out
_mf.parse_action = _parse_action_fixed

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "HobbaReplayStats/1.0 (personal use)"}

# ---------------------------------------------------------------- names/costs
def load_names():
    p = os.path.join(HERE, "aoe2_names.json")
    if not os.path.exists(p):
        return {"units": {}, "buildings": {}, "techs": {}, "civs": {}}
    with open(p, encoding="utf-8") as f:
        return json.load(f)

N = load_names()

def unit_name(i):  return (N["units"].get(str(i)) or [f"Unit #{i}"])[0]
def bld_name(i):   return (N["buildings"].get(str(i)) or [f"Building #{i}"])[0]
def tech_name(i):  return (N["techs"].get(str(i)) or [f"Tech #{i}"])[0]
def civ_name(i):   return N["civs"].get(str(i), f"Civ #{i}")
def cost_of(table, i):
    row = N[table].get(str(i))
    return row[1:5] if row else [0, 0, 0, 0]          # food, wood, gold, stone
def time_of(table, i, default):
    row = N[table].get(str(i))
    return row[5] if row and row[5] else default

# ---------------------------------------------------------------- game ids
VILLAGERS = {83, 293}                      # male / female villager
ECO_UNITS = VILLAGERS | {13, 128, 204}     # + fishing ship, trade cart, trade cog
FEUDAL, CASTLE, IMPERIAL = 101, 102, 103
AGE_TECHS = {FEUDAL: "Feudal", CASTLE: "Castle", IMPERIAL: "Imperial"}
TC_TECHS = {22, 213, 249, 8, 280, FEUDAL, CASTLE, IMPERIAL}   # researched at the Town Center
ECO_TECHS = {22, 213, 249, 202, 203, 221, 14, 13, 12, 55, 182, 278, 279, 8, 280,
             65, 23, 17, 15, 19, 48, 54, 63, 64}
MILITIA, MAA, SCOUT, ARCHER, SKIRM, SPEAR, EAGLE = 74, 75, 448, 4, 7, 93, 751
TOWER, PALISADE, STONE_WALL = 79, 72, 117
# commands that aren't a person clicking (excluded from APM)
NOT_PLAYER = {"GAME", "SAVE", "CHAPTER", "AI_ORDER", "AI_COMMAND", "SPECTATE",
              "POSTGAME", "ERROR", "DE_AUTOSCOUT", "DE_TRANSFORM"}

def mmss(ms):
    if ms is None:
        return "-"
    s = int(round(ms / 1000))
    return f"{s // 60}:{s % 60:02d}"

# ---------------------------------------------------------------- loading
def open_replay(path_or_bytes):
    """Return raw .aoe2record bytes from a path to .aoe2record/.zip, or bytes."""
    raw = path_or_bytes
    if isinstance(path_or_bytes, str):
        with open(path_or_bytes, "rb") as f:
            raw = f.read()
    if raw[:2] == b"PK":   # zip from aoe.ms
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [n for n in z.namelist() if n.lower().endswith((".aoe2record", ".mgz", ".mgx"))]
            if not names:
                raise RuntimeError("The zip has no recorded game inside it.")
            raw = z.read(names[0])
    return raw

def download_replay(game_id, profile_id):
    url = f"https://aoe.ms/replay/?gameId={game_id}&profileId={profile_id}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if len(data) < 1000:
        raise RuntimeError("Replay not available (it may have been removed or never uploaded).")
    return data

def latest_match_id(profile_id):
    url = f"https://data.aoe2companion.com/api/matches?profile_ids={profile_id}&page=1"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    for m in data.get("matches", []):
        if m.get("finished"):
            return m["matchId"]
    raise RuntimeError("No finished match found for that profile.")

# --explore-sync: capture every raw value in the DE sync blocks, to hunt for per-resource numbers
RAW_SYNC = []
def _install_sync_spy():
    import struct
    import mgz.fast as mf
    orig = mf.sync
    def spy(data):
        pos = data.tell()
        out = orig(data)
        try:
            end = data.tell()
            data.seek(pos)
            inc, marker = struct.unpack('<II', data.read(8))
            if not marker:
                _cs, is_de = struct.unpack("<4xI4xI", data.read(16))
                if is_de:
                    data.seek(-16, 1)
                    vals = struct.unpack(f'<{mf.MAX_PLAYERS * mf.SYNC_LEN_PER_PLAYER}I',
                                         data.read(4 * mf.MAX_PLAYERS * mf.SYNC_LEN_PER_PLAYER))
                    RAW_SYNC.append(vals)
            data.seek(end)
        except Exception:
            pass
        return out
    mf.sync = spy

def explore_sync(players):
    if not RAW_SYNC:
        print("No DE sync blocks found.")
        return
    L = 11
    print("\nRaw sync values per player (val 1 = total res; looking for 200/200/100/200 = start food/wood/gold/stone)")
    for snap_i in sorted({i for i in (0, 1, 2, len(RAW_SYNC) // 4, len(RAW_SYNC) // 2) if i < len(RAW_SYNC)}):
        vals = RAW_SYNC[snap_i]
        print(f"\n snapshot {snap_i + 1} of {len(RAW_SYNC)}")
        for ptr in range(0, len(vals), L):
            row = vals[ptr:ptr + L]
            if row[1]:
                name = players.get(row[8], {}).get("name", f"player {row[8]}")
                print(f"  {name[:16]:16} " + " ".join(f"{i}:{v}" for i, v in enumerate(row)))

def read_events(raw):
    """Parse header + body into a simple event list. Times are game time in ms."""
    f = io.BytesIO(raw)
    header = parse_header(f)
    meta(f)
    eof = len(raw)
    t = 0
    events, chat, sync = [], [], []
    while f.tell() < eof:
        try:
            op, payload = operation(f)
        except (EOFError, RuntimeError, ValueError):
            break
        if op == Operation.SYNC:
            inc, _checksum, data = payload
            t += inc
            if data:
                sync.append((t, {k: v["total_res"] for k, v in data.items() if isinstance(k, int)}))
        elif op == Operation.ACTION:
            atype, data = payload
            pid = data.get("player_id")
            if isinstance(pid, (bytes, bytearray)):
                pid = pid[0] if pid else None
            events.append({"t": t, "type": atype.name, "player": pid, **data})
        elif op == Operation.CHAT:
            try:
                chat.append((t, payload.decode("utf-8", errors="replace")))
            except AttributeError:
                pass
    return header, events, chat, sync, t

def _txt(v):
    return v.decode("utf-8", errors="replace") if isinstance(v, (bytes, bytearray)) else v

def players_from_header(h):
    out = {}
    de = (h.get("de") or {}).get("players") or []
    base = {p.get("number"): p for p in (h.get("players") or [])}
    # ...but if other players have real team ids, team 1 is a real team too (2v2 as teams 1 and 2, 5 Nov)
    _t = [p.get("team_id") for p in de if (p.get("number") or 0) >= 1]
    t1_team = _t.count(1) > 1 and any((t or 0) > 1 for t in _t)
    for p in de:
        num = p.get("number")
        if num is None or num < 1:
            continue
        b = base.get(num, {})
        out[num] = {
            "number": num,
            "name": _txt(p.get("name") or b.get("name")) or f"Player {num}",
            "profile_id": p.get("profile_id"),
            # team_id 1 = "-" (no team, usual in custom lobbies like HyperRandom): everyone plays alone, so each gets their own team (Jacob, 29 Oct)
            "team": p.get("team_id") if (p.get("team_id") or 0) > 1 or (p.get("team_id") == 1 and t1_team) else 100 + num,
            "civ": civ_name(p.get("civilization_id", b.get("civilization_id"))),
            "colour": p.get("color_id", b.get("color_id")),
        }
    if not out:  # non-DE fallback
        for num, b in base.items():
            if num and num > 0:
                out[num] = {"number": num, "name": _txt(b.get("name")) or f"Player {num}", "profile_id": None,
                            "team": None, "civ": civ_name(b.get("civilization_id")), "colour": b.get("color_id")}
    return out

# ---------------------------------------------------------------- analysis
def apm_stats(evs, game_ms):
    clicks = [e for e in evs if e["type"] not in NOT_PLAYER]
    eff, last = 0, None
    for e in clicks:
        key = (e["type"], tuple(e.get("object_ids") or ()), e.get("target_id"),
               round(e.get("x") or 0), round(e.get("y") or 0), e.get("unit_id"), e.get("technology_id"))
        if last and key == last[0] and e["t"] - last[1] < 500:
            last = (key, e["t"])
            continue                     # spam / double-click: not "effective"
        eff += 1
        last = (key, e["t"])
    mins = max(game_ms / 60000, 0.01)
    return round(len(clicks) / mins), round(eff / mins), len(clicks)

def tc_idle(evs, age_clicks, game_ms, age_done=None):
    """Simulate the main Town Center's queue from villager queues + TC techs.
    Idle is split by age: Dark (start -> Feudal click), Feudal (Feudal done -> Castle click),
    Castle (Castle done -> Imperial click), Imperial (Imperial done -> game end).
    Time spent researching the age-up itself counts as busy."""
    # Walk the commands in time order. A villager queued with several TCs selected goes to
    # whichever of those TCs frees up first (that's how the game shares a multi-TC queue).
    vil_ms = time_of("units", 83, 25) * 1000
    busy = {}                      # tc id -> busy until (ms)
    windows = defaultdict(list)    # tc id -> idle windows
    first_seen = {}
    def add_job(tc, t, dur):
        first_seen.setdefault(tc, t)
        b = busy.get(tc, 0 if not busy else t)   # starting TC is "on" from 0:00
        if t > b:
            windows[tc].append((b, t))
        busy[tc] = max(b, t) + dur
    for e in sorted(evs, key=lambda e: e["t"]):
        oids = e.get("object_ids") or []
        if not oids:
            continue
        if e["type"] in ("DE_QUEUE", "QUEUE", "MULTIQUEUE") and e.get("unit_id") in VILLAGERS:
            for _ in range(max(e.get("amount") or 1, 0)):
                tc = min(oids, key=lambda o: busy.get(o, 0))
                add_job(tc, e["t"], vil_ms)
        elif e["type"] == "RESEARCH" and e.get("technology_id") in TC_TECHS:
            tid = e["technology_id"]
            add_job(oids[0], e["t"], time_of("techs", tid, 25) * 1000)
    if not busy:
        return None
    main = min(first_seen, key=first_seen.get)     # the TC you started with
    end_at = game_ms
    idle_windows = list(windows[main])
    if busy[main] < end_at:
        idle_windows.append((busy[main], end_at))

    def idle_between(a, b):
        if a is None or b is None or b <= a:
            return None
        return sum(max(0, min(e, b) - max(s, a)) for s, e in idle_windows)

    def done(tid):
        c = age_clicks.get(tid)
        if age_done and tid in age_done:          # exact, from the game's own "advanced to" message
            return age_done[tid]
        return c + time_of("techs", tid, 130) * 1000 if c is not None else None
    f_click, c_click, i_click = age_clicks.get(FEUDAL), age_clicks.get(CASTLE), age_clicks.get(IMPERIAL)
    f_done, c_done, i_done = done(FEUDAL), done(CASTLE), done(IMPERIAL)
    return {
        "dark_ms": idle_between(0, f_click if f_click is not None else end_at),
        "feudal_ms": idle_between(f_done, c_click if c_click is not None else end_at),
        "castle_ms": idle_between(c_done, i_click if i_click is not None else end_at),
        "imperial_ms": idle_between(i_done, end_at),
        "tcs": len(busy),
    }

def opening(evs, age_clicks):
    feudal = age_clicks.get(FEUDAL)
    castle = age_clicks.get(CASTLE)
    pre_feudal = Counter()
    feudal_units, feudal_order = Counter(), []
    towers = 0
    for e in evs:
        t = e["t"]
        if e["type"] in ("DE_QUEUE", "QUEUE", "MULTIQUEUE"):
            u, amt = e.get("unit_id"), max(e.get("amount") or 1, 1)
            if feudal is None or t < feudal:
                pre_feudal[u] += amt
            elif castle is None or t < castle:
                feudal_units[u] += amt
                if u not in feudal_order and u not in ECO_UNITS:
                    feudal_order.append(u)
        if e["type"] == "BUILD" and e.get("building_id") == TOWER and (castle is None or t < castle):
            towers += 1
    tags = []
    if pre_feudal[MILITIA] >= 3:
        tags.append("Drush")
    names = {SCOUT: "Scouts", ARCHER: "Archers", SKIRM: "Skirms", MAA: "M@A", MILITIA: "M@A",
             SPEAR: "Spears", EAGLE: "Eagles"}
    seq = []
    for u in feudal_order:
        n = names.get(u)
        if n and n not in seq:
            seq.append(n)
    if seq:
        tags.append(" > ".join(seq[:2]))
    if towers >= 2:
        tags.append("Towers")
    if not tags and castle is not None:
        tags.append("Fast Castle")
    return " + ".join(tags) if tags else "Unclear"

# earliest believable time (ms) for an age-up to FINISH; the game's own chat can report odd ones (late joiners, takeovers, set start ages)
MIN_AGE_DONE_MS = {101: 150_000, 102: 420_000, 103: 720_000}


def player_report(num, info, evs, game_ms, sync, age_done=None, start_age=0):
    age_done = {k: v for k, v in (age_done or {}).items() if v >= MIN_AGE_DONE_MS.get(k, 0)}
    # ages the game STARTS in (Empire Wars = Feudal, start_age 1) count as reached at 0:00, so no time lands in Dark Age (Jacob, 19 Oct)
    started = [FEUDAL, CASTLE, IMPERIAL][:max(0, min(start_age, 3))]
    age_clicks = {}
    for e in evs:
        if e["type"] == "RESEARCH" and e.get("technology_id") in AGE_TECHS and e["technology_id"] not in age_clicks:
            age_clicks[e["technology_id"]] = e["t"]
    apm, eapm, n_cmds = apm_stats(evs, game_ms)
    # when each age was REACHED (the game's own message, else click + research time); an age still being researched
    # when the game ended was never reached. Per-age eAPM and float split here, so the age-up research counts in the age before (Jacob, 18 Oct)
    reached = {}
    for k, v in age_clicks.items():
        d = age_done.get(k, v + time_of("techs", k, 130) * 1000)
        if k in age_done or d <= game_ms + 5000:
            reached[k] = min(d, game_ms)
    for k in started:
        reached[k] = 0
    # eAPM per age (Dark = start -> Feudal reached, etc.)
    edges = [0] + [reached[k] for k in (FEUDAL, CASTLE, IMPERIAL) if k in reached] + [game_ms]
    eapm_by_age = {}
    for name, a, b in zip(("Dark", "Feudal", "Castle", "Imperial"), edges, edges[1:]):
        if b > a:
            eapm_by_age[name] = apm_stats([e for e in evs if a <= e["t"] < b], b - a)[1]

    units, techs, blds = Counter(), [], Counter()
    spend = {"economy": 0, "military": 0, "techs": 0, "buildings": 0}
    build_order, seen_techs = [], set()
    for e in evs:
        typ, t = e["type"], e["t"]
        if typ in ("DE_QUEUE", "QUEUE", "MULTIQUEUE") and e.get("unit_id") is not None:
            u, amt = e["unit_id"], e.get("amount") or 1
            if amt <= 0:
                continue
            units[u] += amt
            c = sum(cost_of("units", u)) * amt
            spend["economy" if u in ECO_UNITS else "military"] += c
            if len(build_order) < 60 and u not in VILLAGERS:
                prev = build_order[-1] if build_order else None
                # same unit queued again within 45 s: fold into one line ("Scout Cavalry x3")
                if prev and prev[2] == ("u", u) and t - prev[0] < 45000:
                    build_order[-1] = (prev[0], None, ("u", u), prev[3] + amt)
                else:
                    build_order.append((t, None, ("u", u), amt))
        elif typ == "RESEARCH" and e.get("technology_id") is not None:
            tid = e["technology_id"]
            techs.append((t, tid))
            spend["techs"] += sum(cost_of("techs", tid))
            if len(build_order) < 60 and tid not in seen_techs:   # extra clicks on the same tech aren't new steps
                build_order.append((t, f"* {tech_name(tid)}", ("t", tid), 1))
            seen_techs.add(tid)
        elif typ == "BUILD" and e.get("building_id") is not None:
            b = e["building_id"]
            blds[b] += 1
            spend["buildings"] += sum(cost_of("buildings", b))
            if len(build_order) < 60 and b not in (70, 50) and bld_name(b) not in ("Gate", "Palisade Gate"):
                build_order.append((t, f"+ {bld_name(b)}", ("b", b), 1))   # houses/farms/gates skipped to keep it readable
    resign = next((e["t"] for e in evs if e["type"] == "RESIGN"), None)

    # experimental: banked ("floated") resources from sync data - total of all four, not split
    banked = [(t, d[num]) for t, d in sync if num in d]
    float_info = None
    if banked:
        def avg_in(a, b):
            vals = [v for t, v in banked if (a is None or t >= a) and (b is None or t < b)]
            return round(sum(vals) / len(vals)) if vals else None
        f, c, i = reached.get(FEUDAL), reached.get(CASTLE), reached.get(IMPERIAL)
        float_info = {
            "avg_total": avg_in(None, None),
            "avg_dark": avg_in(0, f),
            "avg_feudal": avg_in(f, c) if f is not None else None,
            "avg_castle": avg_in(c, i) if c is not None else None,
            "avg_imperial": avg_in(i, None) if i is not None else None,
            "peak": max(v for _, v in banked),
            "first_reading": banked[0][1],   # standard start = 700 (200F/200W/100G/200S)
        }

    return {
        **info,
        "apm": apm, "eapm": eapm, "commands": n_cmds, "eapm_by_age": eapm_by_age,
        # an age whose estimated finish is after the game ended was never reached (clicked, then the game ended first)
        "ages": {AGE_TECHS[k]: {"clicked": mmss(v),
                                "done_est": mmss(age_done[k]) if k in age_done else "~" + mmss(v + time_of("techs", k, 130) * 1000)}
                 for k, v in sorted(age_clicks.items())
                 if k in age_done or v + time_of("techs", k, 130) * 1000 <= game_ms + 5000},
        "opening": opening(evs, age_clicks),
        "villagers": sum(units[u] for u in VILLAGERS),
        "units": {unit_name(u): n for u, n in units.most_common() if u not in VILLAGERS},
        "techs": [f"{mmss(t)} {tech_name(tid)}" for t, tid in techs],
        "buildings": {bld_name(b): n for b, n in blds.most_common()},
        "spent_estimate": spend,
        "tc_idle_estimate": tc_idle(evs, {**age_clicks, **{k: 0 for k in started}}, game_ms, {**age_done, **{k: 0 for k in started}}),
        "banked_resources_experimental": float_info,
        "resigned_at": mmss(resign) if resign is not None else None,
        # when each economy upgrade was first clicked (seconds); for the post-game ECO UPGRADES page
        "eco_ups": {tid: round(t / 1000) for t, tid in reversed(techs) if tid in ECO_TECHS},
        "build_order": [f"{mmss(t)} " + (s if s else unit_name(k[1]) + (f" x{n}" if n > 1 else ""))
                        for t, s, k, n in sorted(build_order, key=lambda r: r[0])[:40]],   # 40 = what the board holds (was 25, cut off mid-game; Jacob 18 Oct)
    }

def _chat_line(t, m, players):
    """Chat is stored as JSON; turn it into 'name: message'."""
    import re
    try:
        j = json.loads(m)
    except (ValueError, TypeError):
        return f"{mmss(t)} {m}"
    msg = j.get("message") or ""
    if not msg:
        return None
    msg = re.sub(r"<player_id,(\d+),\d+>", lambda g: players.get(int(g.group(1)), {}).get("name", "?"), msg)
    who = players.get(j.get("player"), {}).get("name", f"player {j.get('player')}")
    if "advanced to the" in msg:
        return f"{mmss(t)} ** {msg}"
    return f"{mmss(t)} {who}: {msg}" + (" (taunt)" if (j.get("tauntNumber") or -1) > 0 else "")

def analyse(raw):
    header, events, chat, sync, game_ms = read_events(raw)
    players = players_from_header(header)
    by_player = defaultdict(list)
    for e in events:
        if e.get("player") in players:
            by_player[e["player"]].append(e)
    # the game announces every age-up in chat ("<player_id,8,0> advanced to the Castle Age.") - exact finish times
    import re
    done = defaultdict(dict)
    age_id = {"Feudal": FEUDAL, "Castle": CASTLE, "Imperial": IMPERIAL}
    for t, m in chat:
        g = re.search(r"<player_id,(\d+),\d+> advanced to the (Feudal|Castle|Imperial) Age", m)
        if g and t >= 1000:
            done[int(g.group(1))].setdefault(age_id[g.group(2)], t)
    start_age = (header.get("de") or {}).get("starting_age_id") or 0     # 0 = Dark (standard), 1 = Feudal (Empire Wars), 2 = Castle ...
    reports = [player_report(n, players[n], by_player.get(n, []), game_ms, sync, done.get(n), start_age)
               for n in sorted(players)]
    # result: a team where everyone resigned lost
    teams = defaultdict(list)
    for r in reports:
        teams[r["team"]].append(r)
    # a team lost if everyone resigned, or if most of it resigned while nobody on the other side did
    # (players who drop out or just leave don't record a resign)
    quits = {tm: sum(1 for r in rs if r["resigned_at"]) for tm, rs in teams.items()}
    lost = {tm for tm, rs in teams.items() if rs and quits[tm] == len(rs)}
    if not lost:
        lost = {tm for tm, rs in teams.items()
                if quits[tm] * 2 > len(rs) and all(q == 0 for o, q in quits.items() if o != tm)}
    for r in reports:
        r["result"] = "LOSS" if r["team"] in lost else ("WIN" if lost else "?")
    lobby = header.get("lobby") or {}
    return {
        "game_length": mmss(game_ms),
        "game_length_ms": game_ms,
        "start_age": start_age,
        "map_id": (header.get("scenario") or {}).get("map_id"),
        "population": lobby.get("population"),
        "players": reports,
        # messages stamped 0:00 are stored outside the game timeline (mostly end-of-game chat): list them last
        "chat": ([c for c in (_chat_line(t, m, players) for t, m in chat if t >= 1000) if c]
                 + [c.replace("0:00 ", "--:-- ", 1) for c in (_chat_line(t, m, players) for t, m in chat if t < 1000) if c])[:60],
    }

# ---------------------------------------------------------------- printing
def fmt_res(n):
    return f"{n:,}"

def print_report(res, only=None):
    print(f"\nGame length {res['game_length']}  (game time)\n" + "=" * 60)
    for p in res["players"]:
        if only and only.lower() not in p["name"].lower():
            continue
        print(f"\n{p['name']}  -  {p['civ']}  -  team {p['team']}  -  {p['result']}"
              + (f"  (resigned {p['resigned_at']})" if p["resigned_at"] else ""))
        print("-" * 60)
        print(f"  APM {p['apm']}   eAPM {p['eapm']}   ({p['commands']} commands)")
        if p["eapm_by_age"]:
            print("  eAPM by age: " + ", ".join(f"{k} {v}" for k, v in p["eapm_by_age"].items()))
        ages = "   ".join(f"{k} clicked {v['clicked']} (done {v['done_est']})" for k, v in p["ages"].items())
        print(f"  Ages: {ages or 'stayed in Dark Age'}")
        print(f"  Opening: {p['opening']}")
        print(f"  Villagers queued: {p['villagers']}")
        idle = p["tc_idle_estimate"]
        if idle:
            parts = [f"{k} {mmss(idle[k.lower() + '_ms'])}" for k in ("Dark", "Feudal", "Castle", "Imperial")
                     if idle.get(k.lower() + "_ms") is not None]
            print(f"  TC idle (estimate, main TC): " + ", ".join(parts))
        s = p["spent_estimate"]
        total = sum(s.values())
        if total:
            print("  Spent (estimate): " + ", ".join(f"{k} {fmt_res(v)} ({round(100 * v / total)}%)" for k, v in s.items()))
        fl = p["banked_resources_experimental"]
        if fl:
            bits = [f"{k} {fl['avg_' + k.lower()]}" for k in ("Dark", "Feudal", "Castle", "Imperial")
                    if fl.get("avg_" + k.lower()) is not None]
            print(f"  Avg floated res (experimental): {fl['avg_total']} overall  ({', '.join(bits)}), peak {fl['peak']}")
        if p["units"]:
            print("  Units: " + ", ".join(f"{k} {v}" for k, v in list(p["units"].items())[:10]))
        print("  Build order:")
        for line in p["build_order"][:15]:
            print("    " + line)
    if res["chat"] and not only:
        print("\nChat:")
        for c in res["chat"][:30]:
            print("  " + c)
    print("\nNotes: times are in-game time. TC idle, spending and banked resources are estimates;")
    print("cancelled queues, civ bonuses and being housed aren't visible in a replay.\n")

def main():
    ap = argparse.ArgumentParser(description="AoE2 DE replay stats")
    ap.add_argument("replay", nargs="?", help=".aoe2record or .zip file")
    ap.add_argument("--download", nargs=2, metavar=("GAME_ID", "PROFILE_ID"), help="download from aoe.ms first")
    ap.add_argument("--latest", metavar="PROFILE_ID", help="download this player's newest finished game")
    ap.add_argument("--player", help="only show players whose name contains this")
    ap.add_argument("--json", help="also save all stats to this JSON file")
    ap.add_argument("--save-replay", help="keep the downloaded replay at this path")
    ap.add_argument("--explore-sync", action="store_true",
                    help="print the raw per-player sync numbers (to look for per-resource values)")
    a = ap.parse_args()

    if a.latest:
        gid = latest_match_id(a.latest)
        print(f"Newest finished game: {gid}")
        raw = download_replay(gid, a.latest)
    elif a.download:
        raw = download_replay(*a.download)
    elif a.replay:
        raw = a.replay
    else:
        ap.error("give a replay file, --download GAME_ID PROFILE_ID, or --latest PROFILE_ID")
    if isinstance(raw, bytes) and a.save_replay:
        with open(a.save_replay, "wb") as f:
            f.write(raw)
    if a.explore_sync:
        _install_sync_spy()
    rec = open_replay(raw)
    res = analyse(rec)
    if a.explore_sync:
        hdr = parse_header(io.BytesIO(rec))
        explore_sync(players_from_header(hdr))
    print_report(res, a.player)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2, ensure_ascii=False)
        print(f"Saved {a.json}")

if __name__ == "__main__":
    main()
