"""Tourney Tracker game detector. Runs on a timer (GitHub Action), no PC needed.

For every tournament that is on right now (found through the index collection, one small entry per tournament):
  1. asks aoe2companion for the recent matches of every player that has a profile ID
  2. keeps finished games inside the tournament dates where the tournament's players are most of the lobby
  3. downloads one replay from aoe.ms, parses it (same analyser as the HOBB LAN scout)
  4. writes   tournaments/<site>/pending/<matchId>   (the admin's "Confirm games" queue)
     and      tournaments/<site>/games/<matchId>     (replay stats, public)
Nothing counts until an admin confirms it in the tracker.

Env: FIREBASE_SERVICE_ACCOUNT = the service account key JSON (GitHub secret).
"""
import io, json, os, sys, tempfile, time, zipfile, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

import scout_core as core

UA = {"User-Agent": "Mozilla/5.0 (Tourney Tracker detector)"}
MAX_TRIES = 6            # replay download/parse attempts per game
RETRY_GAP = 900          # wait 15 min x attempts so far before the next attempt (replays can take a while to appear)
MAX_PAGES = 10           # aoe2companion pages per player on the first (catch-up) run
PAUSE = 0.25             # seconds between aoe2companion calls
DAY = 86400


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


# ---------- network (swapped out in tests) ----------
def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


RUN_START = time.time()
FETCH_BUDGET = 9 * 60      # stop paging back after this many seconds of a run; the rest carries on next run
WORK_BUDGET = 15 * 60      # stop downloading replays after this (the Action is killed at 20 minutes, losing everything)


def over(budget):
    return time.time() - RUN_START > budget


def get_retry(get, url):
    """aoe2companion sometimes refuses when called a lot: wait and try again before giving up on a player."""
    for i in range(3):
        try:
            return get(url)
        except Exception:  # noqa: BLE001
            if i == 2 or over(FETCH_BUDGET):
                raise
            time.sleep(4 * (i + 1))


def fetch_matches(pids, start_ts, deep, get=http_get, deep_done=None):
    """Newest matches of each profile id, one entry per match id. deep=True pages back to start_ts for players
    not in deep_done yet. Returns (matches, failed, caught): caught = players fully paged back this run."""
    seen, failed, caught = {}, [], []
    deep_done = set(deep_done or [])
    for pid in pids:
        go_deep = deep and pid not in deep_done and not over(FETCH_BUDGET)
        for page in range(1, (MAX_PAGES if go_deep else 1) + 1):
            try:
                data = json.loads(get_retry(get, f"https://data.aoe2companion.com/api/matches?profile_ids={pid}&page={page}"))
            except Exception as e:  # noqa: BLE001
                log(f"match list for {pid} (page {page}) failed: {e}")
                failed.append(pid)
                go_deep = False
                break
            ms = data.get("matches", [])
            for m in ms:
                if m.get("matchId") is not None:
                    seen[str(m["matchId"])] = m
            oldest = min((core.iso_ts(m.get("started")) or 0 for m in ms), default=0)
            if not ms or oldest < start_ts or page == MAX_PAGES:
                if go_deep:
                    caught.append(pid)
                break
            time.sleep(PAUSE)
        time.sleep(PAUSE)
    return list(seen.values()), failed, caught


def download_replay(match_id, pids, folder, get=http_get):
    path = os.path.join(folder, f"{match_id}.aoe2record")
    for pid in pids:
        try:
            raw = get(f"https://aoe.ms/replay/?gameId={match_id}&profileId={pid}", timeout=60)
        except Exception as e:  # noqa: BLE001
            log(f"  replay {match_id} via {pid}: {e}")
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                raw = z.read(z.namelist()[0])
        except zipfile.BadZipFile:
            pass
        if len(raw) < 1000:
            continue
        with open(path, "wb") as f:
            f.write(raw)
        return path
    return None


# ---------- tournament logic (pure, tested) ----------
def day_ts(d):
    try:
        return datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def tour_window(t, now):
    """(start, end) timestamps games must fall in, or None when the tournament isn't on.
    A day of slack each side covers time zones; no end date = 60 days."""
    start = day_ts(t.get("start"))
    if start is None:
        return None
    end = day_ts(t.get("end")) or (start + 60 * DAY)
    start, end = start - DAY, end + 2 * DAY
    return (start, end) if start <= now <= end + 2 * DAY else None


def elo_window(t, now):
    """ELOs are wanted before a tournament starts too (seeding, predictions): from 21 days before the start."""
    start = day_ts(t.get("start"))
    if start is None:
        return False
    end = day_ts(t.get("end")) or (start + 60 * DAY)
    return start - 21 * DAY <= now <= end + 2 * DAY


def tour_pids(state, t):
    """profile id (int) -> tracker player id, for everyone in the tournament (roster and team members)."""
    ids = set(t.get("roster") or [])
    for tm in t.get("teams") or []:
        ids.update(tm.get("members") or [])
    out = {}
    for i in ids:
        p = (state.get("players") or {}).get(i) or {}
        pid = str(p.get("pid") or "").strip()
        if pid.isdigit():
            out[int(pid)] = i
    return out


def team_of(t):
    """tracker player id -> tournament team id (empty when the tournament has no teams)."""
    return {m: tm.get("id") for tm in t.get("teams") or [] for m in tm.get("members") or []}


def sides_fit(players, teamof):
    """Team tournaments: a real fixture is one tournament team against another. On each side, one team must have
    more players than any other team and be at least half of that side; anyone else on the side is a fill-in (another
    team's player or someone outside the tournament), which the organiser confirms by hand. The two sides must be
    different teams.
    players: [{"team": lobby side, "id": tracker player id or None}]. Tournaments without teams always fit."""
    if len(set(teamof.values())) < 2:
        return True
    sizes = {}
    for tm in teamof.values():
        sizes[tm] = sizes.get(tm, 0) + 1
    need = 2 if min(sizes.values()) >= 2 else 1     # real teams: a 1v1 between two of their players is not a fixture
    sides = {}
    for p in players:
        sides.setdefault(p.get("team"), []).append(p)
    if len(sides) != 2:
        return False
    found = []
    for ps in sides.values():
        tms = [teamof.get(p.get("id")) for p in ps if p.get("id")]
        tms = [x for x in tms if x]
        cnt = sorted(((tms.count(x), x) for x in set(tms)), reverse=True)
        if not cnt or cnt[0][0] < need or cnt[0][0] * 2 < len(ps) or (len(cnt) > 1 and cnt[1][0] == cnt[0][0]):
            return False
        found.append((cnt[0][1], set(tms)))
    # different teams, and nobody plays against their own team (a fill-in comes from a third team or outside)
    return found[0][0] != found[1][0] and found[0][0] not in found[1][1] and found[1][0] not in found[0][1]


def ranked(leaderboard):
    """True for matchmaking ladders (Random Map, Team Random Map, Empire Wars...). Tournament games are lobby games,
    which aoe2companion labels Unranked; a missing label is given the benefit of the doubt."""
    lb = str(leaderboard or "").strip().lower()
    return bool(lb) and "unranked" not in lb


def why_not(summary, pidmap, window, teamof=None):
    """None when the game is a candidate, otherwise a short reason code:
    few (fewer than 2 tournament players), ranked, unfinished, dates, lobby (tournament players are under half
    the lobby), teams (the two sides aren't two of the tournament's teams)."""
    ps = summary.get("players") or []
    ours = [p for p in ps if p.get("profileId") in pidmap]
    st = summary.get("started") or 0
    if len(ours) < 2:
        return "few"
    if not window[0] <= st <= window[1]:
        return "dates"
    if ranked(summary.get("leaderboard")):
        return "ranked"
    if not summary.get("finished"):
        return "unfinished"
    if len(ours) * 2 < len(ps):
        return "lobby"
    if not sides_fit([{"team": p.get("team"), "id": pidmap.get(p.get("profileId"))} for p in ps], teamof or {}):
        return "teams"
    return None


def is_candidate(summary, pidmap, window, teamof=None):
    """Finished, inside the dates, 2+ tournament players and they are at least half the lobby.
    In a team tournament the two sides must also be two of its teams (keeps ranked games between mates out)."""
    return why_not(summary, pidmap, window, teamof) is None


def fill_winner(game):
    """aoe2companion sometimes has no winner for lobby games: use the replay (result, else who resigned)."""
    ps = game.get("players") or []
    if not ps or all(p.get("won") is not None for p in ps):
        return
    res = [(p.get("stats") or {}).get("result") for p in ps]
    if all(r in ("WIN", "LOSS") for r in res):
        for p, r in zip(ps, res):
            p["won"] = r == "WIN"
        return
    teams = {}
    for p in ps:
        teams.setdefault(p.get("team"), []).append(p)
    if len(teams) != 2 or None in teams:
        return
    quit_ = {t: [bool((p.get("stats") or {}).get("resigned")) for p in tp] for t, tp in teams.items()}
    lost = [t for t, q in quit_.items() if all(q)]
    if len(lost) == 1:
        for p in ps:
            p["won"] = p.get("team") != lost[0]


# ---------- restored games (a game that dropped and was loaded from a save to play it out; Jacob, 22 Nov) ----------
# Each part has its own match ID and replay. The restored replay's clock starts at 0:00 from the save, so its times
# are moved on by the save time, and its opening is dropped (it starts mid-game). The organiser joins the parts into
# one fixture game in Confirm games: the result comes from the last part, the stats from both.
TWIN_GAP = 6 * 3600       # parts of one game start within 6 hours of each other


def _shift(v, by):
    x = secs(v)
    return core.mmss(x + by) if x is not None else v


def restore_shift(stats):
    """Move a restored replay's times on to real game time. Returns the save time in seconds (None = normal game)."""
    rms = (stats or {}).get("restoreMs")
    if rms is None:
        return None
    by = round(rms / 1000)
    stats["partTime"] = stats.get("gameTime")
    if by:
        stats["gameTime"] = _shift(stats.get("gameTime"), by)
    for p in stats.get("players") or []:
        p.pop("opening", None)
        p.pop("douche", None)
        if not by:
            continue
        for k in ("ages", "agesClicked", "agesDone"):
            if isinstance(p.get(k), dict):
                p[k] = {a: _shift(v, by) for a, v in p[k].items()}
        for k in ("build", "techs", "eco"):
            if isinstance(p.get(k), list):
                p[k] = [[_shift(e[0], by)] + list(e[1:]) if isinstance(e, (list, tuple)) and e else e for e in p[k]]
        if p.get("resigned"):
            p["resigned"] = _shift(p["resigned"], by)
    return by


def twin_key(s):
    """Same players (by profile ID) on the same map: the parts of a restored game share this."""
    return (tuple(sorted(str(p.get("profileId")) for p in s.get("players") or [])), norm_map(s.get("map")))


def twins(summaries):
    """matchId -> (earlier part or None, later part or None) among games with the same players and map."""
    by = {}
    for s in summaries:
        by.setdefault(twin_key(s), []).append(s)
    out = {}
    for group in by.values():
        group.sort(key=lambda x: x.get("started") or 0)
        for i, s in enumerate(group):
            st = s.get("started") or 0
            prev = group[i - 1] if i and st - (group[i - 1].get("started") or 0) <= TWIN_GAP else None
            nxt = group[i + 1] if i + 1 < len(group) and (group[i + 1].get("started") or 0) - st <= TWIN_GAP else None
            out[str(s.get("matchId"))] = (str(prev["matchId"]) if prev else None, str(nxt["matchId"]) if nxt else None)
    return out


def pending_doc(game, tour_id, pidmap, stats_ok, tries):
    return {
        "matchId": str(game.get("matchId")), "tour": tour_id, "status": "new",
        "started": game.get("started"), "finished": game.get("finished"),
        "map": game.get("map"), "leaderboard": game.get("leaderboard"),
        "gameTime": game.get("gameTime"),
        "players": [{"pid": p.get("profileId"), "id": pidmap.get(p.get("profileId")), "name": p.get("name"),
                     "team": p.get("team"), "civ": p.get("civ"), "won": p.get("won"), "color": p.get("color"),
                     "sum": headline(p.get("stats"))} for p in game.get("players") or []],
        "stats": bool(stats_ok), "tries": tries,
        **({"restore": {k: v for k, v in game["restore"].items() if v is not None}} if game.get("restore") else {}),
    }  # "tried" (time of the last replay attempt) is added by run_site


def headline(st):
    """The few numbers copied into the tracker save when a game is confirmed (full stats stay in games/)."""
    if not st:
        return None
    ages = st.get("ages") or {}
    idle, det = st.get("tcIdle") or {}, st.get("detail") or {}
    return {k: v for k, v in {
        "eapm": st.get("eapm"), "apm": st.get("apm"), "opening": st.get("opening"),
        "feudal": ages.get("Feudal"), "castle": ages.get("Castle"), "imp": ages.get("Imperial"),
        "vils": st.get("vils"), "mil": st.get("military"),
        "tcIdle": sum((st.get("tcIdle") or {}).values()) if st.get("tcIdle") else None,
        # TC idle from the start until Castle Age is clicked (Dark Age + Feudal Age idle)
        "idleC": (idle.get("dark") or 0) + (idle.get("feudal") or 0) if idle and ("dark" in idle or "feudal" in idle) else None,
        "peak": det.get("eapmPeak"), "milPct": det.get("focusMilPct"), "ecoPct": det.get("focusEcoPct"),
        "flt": st.get("floatAvg"), "fltPk": st.get("floatPeak"),
        # TC douche: "D" = Dark Age, "F" = Feudal; "!" = placed in range of the enemy TC
        "douche": ",".join(d["age"][0] + ("!" if d.get("in_range") else "") for d in st["douche"]) if st.get("douche") else None,
    }.items() if v is not None}


# ---------- auto-confirm (optional per tournament: t["autoConfirm"]) ----------
AUTO_MIN_SECS = 5 * 60      # shorter games are often restarts: leave them for an organiser


def secs(v):
    try:
        parts = [int(x) for x in str(v or "").split(":")]
    except ValueError:
        return None
    if not parts or len(parts) > 3:
        return None
    n = 0
    for x in parts:
        n = n * 60 + x
    return n


def norm_map(v):
    return "".join(ch for ch in str(v or "").lower() if ch.isalnum())


def ent_members(k, t):
    kind, _, i = str(k).partition(":")
    if kind == "p":
        return [i]
    tm = next((x for x in t.get("teams") or [] if x.get("id") == i), None)
    return list(tm.get("members") or []) if tm else []


def series_done(S, m):
    """True when the series already has its winner (or every map of a play-all series is in)."""
    bo = m.get("bo") or S.get("bo") or 1
    ws = [g.get("w") for g in m.get("games") or [] if g and g.get("w") in (0, 1)]
    if S.get("playAll"):
        return len(ws) >= bo
    return max(ws.count(0), ws.count(1)) >= bo // 2 + 1


def auto_pick(t, pdoc):
    """(reason, fit): fit = (stage, match, flip, winner side of the game) when this detected game belongs to exactly
    one open round-robin / Swiss fixture with a known winner; otherwise reason says why it waits for an organiser."""
    if pdoc.get("restore"):
        return "restored", None      # part of a game that dropped and was restored: an organiser joins the parts
    if secs(pdoc.get("gameTime")) is not None and secs(pdoc.get("gameTime")) < AUTO_MIN_SECS:
        return "short", None
    sides = {}
    for pl in pdoc.get("players") or []:
        sides.setdefault(pl.get("team"), []).append(pl)
    if len(sides) != 2:
        return "sides", None
    ps = list(sides.values())
    won = [any(pl.get("won") is True for pl in side) for side in ps]
    lost = [all(pl.get("won") is False for pl in side) for side in ps]
    if won[0] == won[1] or not (lost[0] or lost[1]):
        return "winner", None
    w = 0 if won[0] else 1
    ids = [[pl.get("id") for pl in side if pl.get("id")] for side in ps]
    if not ids[0] or not ids[1]:
        return "players", None
    if any(not pl.get("id") for side in ps for pl in side):
        return "fill-in", None     # someone outside the tournament played: an organiser confirms it as a fill-in game
    fits = []
    for S in t.get("stages") or []:
        if S.get("type") not in ("rr", "swiss"):
            continue
        for m in S.get("matches") or []:
            sd = m.get("sides") or []
            if len(sd) != 2 or not sd[0] or not sd[1] or series_done(S, m):
                continue
            played = {norm_map(g.get("map")) for g in m.get("games") or [] if g and g.get("w") in (0, 1) and g.get("map")}
            if norm_map(pdoc.get("map")) in played:
                continue   # same map again in this series: likely a restart or a mistake, so leave it for an organiser
            mem = [[x for k in side for x in ent_members(k, t)] for side in sd]
            if all(x in mem[0] for x in ids[0]) and all(x in mem[1] for x in ids[1]):
                fits.append((S, m, False, w))
            elif all(x in mem[1] for x in ids[0]) and all(x in mem[0] for x in ids[1]):
                fits.append((S, m, True, w))
    if len(fits) != 1:
        return ("none" if not fits else "several"), None
    return None, fits[0]


def auto_game(S, m, flip, w, pdoc):
    """The game record the tracker stores (same shape as an organiser's Confirm)."""
    mp = pdoc.get("map") or ""
    pool = next((x for x in S.get("maps") or [] if norm_map(x) == norm_map(mp)), None)
    g = {"w": (1 - w) if flip else w, "map": pool or mp, "civs": {}, "mid": str(pdoc.get("matchId")), "auto": True}
    if pdoc.get("gameTime"):
        g["len"] = pdoc["gameTime"]
    for pl in pdoc.get("players") or []:
        i = pl.get("id")
        if not i:
            continue
        if pl.get("civ"):
            g["civs"][i] = pl["civ"]
        if pl.get("sum"):
            g.setdefault("st", {})[i] = pl["sum"]
    return g


def auto_confirm(db, site_ref, tour_id, mid):
    """Put the game into its fixture inside a transaction, and mark the queued game confirmed. Returns a log line."""
    from firebase_admin import firestore
    pref = site_ref.collection("pending").document(mid)

    @firestore.transactional
    def run(tx):
        snap = site_ref.get(transaction=tx)
        pd = pref.get(transaction=tx).to_dict() or {}
        if pd.get("status") != "new":
            return "already handled"
        data = snap.to_dict() or {}
        state = json.loads(data.get("state") or "{}")
        t = next((x for x in state.get("tours") or [] if x.get("id") == tour_id), None)
        if not t or not t.get("autoConfirm"):
            return "auto-confirm is off"
        by = {str(p.get("pid") or "").strip(): k for k, p in (state.get("players") or {}).items() if str(p.get("pid") or "").strip()}
        for pl in pd.get("players") or []:      # profile IDs as they are now (an organiser may have fixed one)
            k = by.get(str(pl.get("pid") or pl.get("profileId") or ""))
            if k:
                pl["id"] = k
        why, fit = auto_pick(t, pd)
        if not fit:
            return f"left for an organiser ({why})"
        S, m, flip, w = fit
        games = m.setdefault("games", [])
        if any(str((g or {}).get("mid")) == str(mid) for g in games):
            return "already in the fixture"
        i = next((k for k, g in enumerate(games) if not g or g.get("w") not in (0, 1)), len(games))
        had = games[i] if i < len(games) else {}
        g = auto_game(S, m, flip, w, pd)
        if (had or {}).get("map") and not g.get("map"):
            g["map"] = had["map"]
        merged = dict(had or {})
        merged.update(g)
        if i < len(games):
            games[i] = merged
        else:
            games.append(merged)
        tx.update(site_ref, {"state": json.dumps(state, separators=(",", ":"), ensure_ascii=False), "updated": firestore.SERVER_TIMESTAMP})
        tx.update(pref, {"status": "confirmed", "by": "auto", "at": firestore.SERVER_TIMESTAMP, "fixture": {"stage": S.get("id"), "match": m.get("id"), "game": i}})
        return f"auto-confirmed into {S.get('name')} / {m.get('id')} map {i + 1}"
    return run(db.transaction())


# ---------- ranked ELOs (shown on the stats pages; ranked games themselves never count as fixtures) ----------
ELO_EVERY = 6 * 3600          # seconds between refreshes of a tournament's ELOs
LADDERS = ("rm1", "rmt", "ew1", "ewt")


def ladder_key(*labels):
    """aoe2companion leaderboard id / name -> rm1, rmt, ew1, ewt (None for anything else: unranked, DM, console...)."""
    s = " ".join(str(x) for x in labels if x).lower().replace("-", "_")
    if not s or any(w in s for w in ("unranked", "console", "xbox", "playstation", "death", "dm_", "ror", "return of rome", "quick")):
        return None
    words = s.replace("_", " ").split()
    team = "team" in words or "tg" in words
    if "ew" in words or "empire" in words:
        return "ewt" if team else "ew1"
    if "rm" in words or "random" in words:
        return "rmt" if team else "rm1"
    return None


def _num(v):
    try:
        n = int(round(float(v)))
        return n if 0 < n < 5000 else None
    except (TypeError, ValueError):
        return None


def parse_profile_elos(data):
    """Profile reply -> {ladder: {"r": rating, "max": peak}}. Tolerant of the shape, which we can't pin down in tests."""
    out = {}
    if isinstance(data, dict) and isinstance(data.get("profile"), dict):
        data = data["profile"]
    for lb in (data.get("leaderboards") if isinstance(data, dict) else None) or []:
        if not isinstance(lb, dict):
            continue
        k = ladder_key(lb.get("leaderboardId"), lb.get("leaderboardName") if not lb.get("leaderboardId") else None, lb.get("abbreviation") if not lb.get("leaderboardId") else None)
        r = _num(lb.get("rating"))
        if k and r and k not in out:
            out[k] = {"r": r}
            mx = _num(lb.get("maxRating"))
            if mx:
                out[k]["max"] = max(mx, r)
    return out


def elos_from_matches(matches):
    """Fallback: each player's rating after their newest ranked game per ladder, from match lists already fetched."""
    best = {}
    for m in matches:
        k = ladder_key(m.get("leaderboardId"), m.get("leaderboardName") if not m.get("leaderboardId") else None)
        ts = core.iso_ts(m.get("started")) or 0
        if not k:
            continue
        for t in m.get("teams") or []:
            for p in t.get("players") or []:
                r = _num(p.get("rating"))
                if r is None or p.get("profileId") is None:
                    continue
                d = p.get("ratingDiff")
                r = r + int(d) if isinstance(d, (int, float)) else r
                cur = best.setdefault(p["profileId"], {})
                if k not in cur or ts > cur[k][0]:
                    cur[k] = (ts, r)
    return {pid: {k: {"r": v[1]} for k, v in d.items()} for pid, d in best.items()}


def _cc(v):
    """Two-letter country code as aoe2companion gives it ("au"), or None."""
    v = str(v or "").strip().lower()
    return v if len(v) == 2 and v.isalpha() else None


def countries_from_matches(matches):
    """profile id -> country code, from the player lists of any games already fetched (ranked or lobby)."""
    out = {}
    for m in matches:
        for t in m.get("teams") or []:
            for p in t.get("players") or []:
                c = _cc(p.get("country"))
                if c and p.get("profileId") is not None:
                    out[p["profileId"]] = c
    return out


def fetch_elos(pids, get=http_get, cc=None):
    """profile id -> {ladder: {...}} from each player's aoe2companion profile. Players whose profile can't be read are left out.
    When cc is a dict, each profile's country code is put in it too (profile id -> "au")."""
    out, bad = {}, 0
    for pid in pids:
        try:
            data = json.loads(get(f"https://data.aoe2companion.com/api/profiles/{pid}"))
            if cc is not None and isinstance(data, dict):
                c = _cc((data.get("profile") if isinstance(data.get("profile"), dict) else data).get("country"))
                if c:
                    cc[pid] = c
            e = parse_profile_elos(data)
            if e:
                out[pid] = e
            elif not bad:
                keys = sorted(data)[:25] if isinstance(data, dict) else type(data).__name__
                log(f"  ELO: no ranked ladders read for {pid}; reply had: {keys}")
                bad += 1
        except Exception as e:  # noqa: BLE001
            if not bad:
                log(f"  ELO: profile for {pid} failed: {e}")
            bad += 1
        time.sleep(PAUSE)
    return out


WE_LADDERS = {3: "rm1", 4: "rmt", 13: "ew1", 14: "ewt"}   # the game's own leaderboard ids


def fetch_we(pids, get=http_get):
    """Backup straight from the game's own API (World's Edge, which aoe2companion reads too), for players aoe2companion
    doesn't have (often ones who haven't played for a while). profile id -> {"cc": "au", "elo": {ladder: {"r", "max"}}}."""
    out = {}
    pids = list(pids)
    for n in range(0, len(pids), 40):
        chunk = pids[n:n + 40]
        try:
            ids = urllib.parse.quote(json.dumps([int(p) for p in chunk], separators=(",", ":")))
            data = json.loads(get(f"https://aoe-api.worldsedgelink.com/community/leaderboard/GetPersonalStat?title=age2&profile_ids={ids}"))
        except Exception as e:  # noqa: BLE001
            log(f"  World's Edge backup failed: {e}")
            continue
        group = {}
        for g in data.get("statGroups") or []:
            for m in g.get("members") or []:
                pid = m.get("profile_id")
                if pid in chunk and g.get("type", 1) == 1:
                    group[g.get("id")] = pid
                    o = out.setdefault(pid, {"elo": {}})
                    if _cc(m.get("country")):
                        o["cc"] = _cc(m.get("country"))
        for st in data.get("leaderboardStats") or []:
            pid, k, r = group.get(st.get("statgroup_id")), WE_LADDERS.get(st.get("leaderboard_id")), _num(st.get("rating"))
            if pid is None or not k or not r:
                continue
            e = out[pid]["elo"][k] = {"r": r}
            mx = _num(st.get("highestrating"))
            if mx:
                e["max"] = max(mx, r)
        time.sleep(PAUSE)
    return out


def update_elos(site_ref, state, matches_by_pid, now, get=http_get):
    """Keep tournaments/<tid>/detector/elo fresh: {at, p: {tracker player id: {ladder: {r, max}}}, cc: {tracker player id: "au"}}."""
    pidmap = {}
    for t in state.get("tours") or []:
        if elo_window(t, now):
            pidmap.update(tour_pids(state, t))
    if not pidmap:
        return
    ref = site_ref.collection("detector").document("elo")
    cur = ref.get().to_dict() or {}
    have = cur.get("p") or {}
    fresh = now - (cur.get("at") or 0) < ELO_EVERY
    asked = set(cur.get("asked") or []) if fresh else set()      # players already asked since the last full refresh
    need = sorted(pid for pid, i in pidmap.items() if not fresh or (i not in have and i not in asked))
    fallback = elos_from_matches(matches_by_pid)
    ccs = dict(cur.get("cc") or {})
    got_cc = countries_from_matches(matches_by_pid)
    got = fetch_elos(need, get, got_cc) if need else {}
    miss = [pid for pid in need if not got.get(pid) or pid not in got_cc]      # aoe2companion had nothing (or no country)
    we = fetch_we(miss, get) if miss else {}
    for pid, w in we.items():
        if w.get("cc") and pid not in got_cc:
            got_cc[pid] = w["cc"]
        if w.get("elo"):
            got[pid] = {**w["elo"], **(got.get(pid) or {})}             # aoe2companion's numbers win where both have one
    if miss:
        log(f"{site_ref.id}: World's Edge backup asked for {len(miss)} player(s), {sum(1 for w in we.values() if w.get('cc') or w.get('elo'))} found")
    changed = False
    for pid, i in pidmap.items():                           # nation flags: country code from aoe2companion
        if got_cc.get(pid) and ccs.get(i) != got_cc[pid]:
            ccs[i] = got_cc[pid]
            changed = True
    for pid, i in pidmap.items():
        e = dict(have.get(i) or {})
        for k, v in (fallback.get(pid) or {}).items():      # newest ranked game seen this run
            if k not in got.get(pid, {}):
                e[k] = {**(e.get(k) or {}), **v}
                if e[k].get("max") and e[k]["max"] < e[k]["r"]:
                    e[k]["max"] = e[k]["r"]
        e.update(got.get(pid) or {})
        if e and e != have.get(i):
            have[i] = e
            changed = True
    if need or changed:
        asked.update(pidmap[pid] for pid in need)
        ref.set({"at": cur.get("at") or 0 if fresh else now, "p": have, "asked": sorted(asked), "cc": ccs})
        log(f"{site_ref.id}: ELOs saved for {len(have)} players ({len(got)} of {len(need)} profiles read)")


# ---------- one site ----------
STAT_KEYS = ("apm", "eapm", "opening", "ages", "agesClicked", "agesDone", "build", "techs", "eco", "units", "vils", "military", "walls",
             "resigned", "tcIdle", "floatAvg", "floatPeak", "spent", "detail", "douche")


def state_resum(state, tour_id, sums):
    """Put re-parsed headline numbers into the confirmed fixture games. sums: matchId -> {profile id: headline}.
    Games joined from several replays (restored from a save) are left alone. Returns how many games changed."""
    by = {str(p.get("pid") or "").strip(): k for k, p in (state.get("players") or {}).items() if str(p.get("pid") or "").strip()}
    t = next((x for x in state.get("tours") or [] if x.get("id") == tour_id), None)
    n = 0
    for S in (t or {}).get("stages") or []:
        for m in S.get("matches") or []:
            for g in m.get("games") or []:
                if not g or g.get("parts") or str(g.get("mid")) not in sums or not isinstance(g.get("st"), dict):
                    continue
                hit = False
                for pid, sm in sums[str(g.get("mid"))].items():
                    k = by.get(str(pid))
                    if k and k in g["st"] and sm:
                        g["st"][k] = sm
                        hit = True
                n += hit
    return n


def map_name(summary, stats):
    """The map played, as core.merge picks it: aoe2companion's name for a built-in map (the replay's file name can be
    left over from an earlier custom pick in the lobby), the replay's for a custom map, else the replay's map number."""
    summary, stats = summary or {}, stats or {}
    if summary.get("map") and not summary.get("mapCustom"):
        return summary["map"]
    if summary.get("map"):
        return stats.get("map") or summary["map"]
    return stats.get("mapBuiltIn") or stats.get("map")


def state_remap(state, tour_id, maps):
    """Correct confirmed fixture games' map names after a re-parse. maps: matchId -> (old name, new name). Only a game
    whose map is still the old automatic name (or empty) changes, so a name an organiser typed in is kept."""
    t = next((x for x in state.get("tours") or [] if x.get("id") == tour_id), None)
    n = 0
    for S in (t or {}).get("stages") or []:
        for m in S.get("matches") or []:
            for g in m.get("games") or []:
                hit = maps.get(str((g or {}).get("mid")))
                if not hit or (g.get("map") and norm_map(g["map"]) != norm_map(hit[0])):
                    continue
                pool = next((x for x in S.get("maps") or [] if norm_map(x) == norm_map(hit[1])), None)
                g["map"] = pool or hit[1]
                n += 1
    return n


def reparse_tour(db, site_ref, t, meta, pidmap, now, get=http_get):
    """Organiser pressed "Re-parse replays" (t.reparse = when): download every stored game's replay again, parse it
    with the current parser, and update the game stats, the queue and the confirmed fixture games. Spread over runs."""
    from firebase_admin import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter
    ask = str(t.get("reparse") or "")
    if not ask:
        return
    allrp = meta.setdefault("reparse", {})
    rp = allrp.get(t["id"]) or {}
    if rp.get("asked") != ask:
        rp = {"asked": ask, "done": [], "failed": []}
    allrp[t["id"]] = rp
    if rp.get("finished"):
        return
    pend = {}
    for d in site_ref.collection("pending").where(filter=FieldFilter("tour", "==", t["id"])).stream():
        pd = d.to_dict() or {}
        if pd.get("status") in ("new", "confirmed", "joined") and pd.get("stats"):
            pend[d.id] = pd
    todo = [m for m in sorted(pend) if m not in rp["done"] and m not in rp["failed"]]
    rp["total"] = len(pend)
    log(f"  re-parse: {len(todo)} of {len(pend)} games left")
    sums, maps = {}, {}
    for mid in todo:
        if over(WORK_BUDGET):
            log("  re-parse: out of time this run, next run carries on")
            break
        gref = site_ref.collection("games").document(mid)
        gs = gref.get()
        try:
            game = json.loads((gs.to_dict() or {}).get("json") or "{}") if gs.exists else {}
        except ValueError:
            game = {}
        pids = [p.get("pid") for p in pend[mid].get("players") or [] if p.get("pid") in pidmap] or \
               [p.get("pid") for p in pend[mid].get("players") or [] if p.get("pid")]
        stats = None
        with tempfile.TemporaryDirectory() as tmp:
            path = download_replay(mid, pids, tmp, get)
            if path:
                try:
                    stats = core.parse_replay(path)
                except Exception as e:  # noqa: BLE001
                    log(f"  game {mid}: parse failed ({e})")
        try:     # the map name again, with the current rule (22 Nov: Arena had been saved as "AD5 Dry Socotra")
            summ = core.summarize_match(json.loads(get_retry(get, f"https://data.aoe2companion.com/api/matches/{mid}")))
        except Exception:  # noqa: BLE001
            summ = {}
        was = game.get("map") or pend[mid].get("map")
        # without a replay only a built-in name from aoe2companion is sure enough to change it
        mp = map_name(summ, stats) if stats else (summ.get("map") if not summ.get("mapCustom") else None)
        if mp and norm_map(mp) != norm_map(was):
            maps[mid] = (was or "", mp)
            log(f"  game {mid}: map {was!r} -> {mp!r}")
        if not stats or not game.get("players"):
            rp["failed"].append(mid)
            log(f"  game {mid}: re-parse failed (no replay or no stored game), old stats kept")
            if mid in maps:
                if game.get("players"):
                    game["map"] = mp
                    gref.set({"json": json.dumps(game, separators=(",", ":")), "matchId": mid, "started": game.get("started"),
                              "updated": firestore.SERVER_TIMESTAMP}, merge=True)
                site_ref.collection("pending").document(mid).set({"map": mp}, merge=True)
            continue
        restore_shift(stats)
        if mid in maps:
            game["map"] = mp
        new = {p["profileId"]: p for p in stats.get("players") or []}
        for p in game["players"]:
            sp = new.get(p.get("profileId"))
            if sp:
                p["stats"] = {k: sp[k] for k in STAT_KEYS if k in sp}
        body = json.dumps(game, separators=(",", ":"))
        if len(body) >= 950000:
            rp["failed"].append(mid)
            continue
        gref.set({"json": body, "matchId": mid, "started": game.get("started"), "updated": firestore.SERVER_TIMESTAMP, "reparsed": now})
        hs = {p.get("profileId"): headline(p.get("stats")) for p in game["players"] if p.get("stats")}
        pl = pend[mid].get("players") or []
        for p in pl:
            if p.get("pid") in hs:
                p["sum"] = hs[p["pid"]]
        site_ref.collection("pending").document(mid).set({"players": pl, **({"map": mp} if mid in maps else {})}, merge=True)
        sums[mid] = hs
        rp["done"].append(mid)
        log(f"  game {mid}: re-parsed")
    if sums or maps:
        @firestore.transactional
        def run(tx):
            snap = site_ref.get(transaction=tx)
            state = json.loads((snap.to_dict() or {}).get("state") or "{}")
            n = state_resum(state, t["id"], sums) + state_remap(state, t["id"], maps)
            if n:
                tx.update(site_ref, {"state": json.dumps(state, separators=(",", ":"), ensure_ascii=False), "updated": firestore.SERVER_TIMESTAMP})
            return n
        try:
            log(f"  re-parse: {run(db.transaction())} fixture games updated")
        except Exception as e:  # noqa: BLE001
            log(f"  re-parse: could not update the fixtures ({e}); next run tries again")
            rp["done"] = [m for m in rp["done"] if m not in sums]
            rp["failed"] = [m for m in rp["failed"] if m not in maps]
    if not [m for m in pend if m not in rp["done"] and m not in rp["failed"]]:
        rp["finished"] = now
        log(f"  re-parse finished: {len(rp['done'])} games, {len(rp['failed'])} failed")


def run_site(db, site_ref, now=None, get=http_get):
    from firebase_admin import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter
    now = now or time.time()
    snap = site_ref.get()
    data = snap.to_dict() or {}
    try:
        state = json.loads(data.get("state") or "{}")
    except ValueError:
        log(f"{site_ref.id}: save can't be read, skipped")
        return 0
    meta_ref = site_ref.collection("detector").document("meta")
    meta = meta_ref.get().to_dict() or {}
    # what we already queued lives in the meta record (one read per run instead of one per queued game)
    known = meta.setdefault("known", {})   # matchId -> {"stats": bool, "tries": n, "tried": ts}
    added = 0
    seen_matches = []
    for t in state.get("tours") or []:
        window = tour_window(t, now)
        pidmap = tour_pids(state, t)
        if not window or len(pidmap) < 2:
            continue
        deep = not (meta.get("caughtUp") or {}).get(t["id"])
        log(f"{site_ref.id} / {t.get('name')}: {len(pidmap)} players with profile IDs{' (catch-up)' if deep else ''}")
        teamof = team_of(t)
        # games queued under an older, looser rule (or before teams were entered) that no longer fit: take them out
        # of the queue, unless an admin has already dealt with them
        recheck = set()   # queued games an organiser asked to fetch and parse again ("Re-check" in Confirm games)
        for d in site_ref.collection("pending").where(filter=FieldFilter("status", "==", "new")).stream():
            pd = d.to_dict() or {}
            if pd.get("tour") == t["id"] and pd.get("recheck"):
                recheck.add(d.id)
            if pd.get("tour") == t["id"] and (ranked(pd.get("leaderboard")) or not sides_fit(pd.get("players") or [], teamof)):
                recheck.discard(d.id)
                d.reference.delete()
                site_ref.collection("games").document(d.id).delete()
                known[d.id] = {"stats": True, "tries": MAX_TRIES, "tried": now, "skip": True}
                log(f"  game {d.id}: ranked or not team v team, removed from the queue")
        done_before = ((meta.get("deepDone") or {}).get(t["id"])) or []
        matches, failed, caught = fetch_matches(sorted(pidmap), window[0], deep, get, done_before)
        if deep:
            dd = sorted(set(done_before) | set(caught))
            meta.setdefault("deepDone", {})[t["id"]] = dd
            log(f"  catch-up: {len(dd)} of {len(pidmap)} players paged back so far")
        seen_matches += matches
        have = {str(m.get("matchId")) for m in matches}
        for mid in sorted(recheck - have):   # older than this run looked back: fetch that one game
            try:
                matches.append(json.loads(get_retry(get, f"https://data.aoe2companion.com/api/matches/{mid}")))
            except Exception as e:  # noqa: BLE001
                log(f"  game {mid}: re-check could not fetch it ({e})")
        rep = {"at": now, "players": len(pidmap), "looked": len(matches), "fit": 0, "skipped": {}, "failed": len(failed)}
        meta.setdefault("report", {})[t["id"]] = rep
        left = meta.setdefault("left", {}).setdefault(t["id"], {})
        sums = [core.summarize_match(m) for m in matches]
        tw = twins(sums)
        for s in sums:
            mid = str(s["matchId"])
            old = known.get(mid)
            why = why_not(s, pidmap, window, teamof)
            prev, later = tw.get(mid, (None, None))
            if why == "unfinished" and later:
                why = None        # a game that dropped and was played again (restored or restarted): queue it so it can be joined
            if why:
                if why not in ("few", "dates"):      # a game between tournament players inside the dates: say why it was left out
                    if left.get(mid) != why:
                        names = " v ".join(", ".join(str(p.get("name") or "?") for p in s["players"] if p.get("team") == tm) for tm in sorted({p.get("team") for p in s["players"]}, key=str))
                        log(f"  game {mid} left out ({why}): {s.get('leaderboard') or 'no label'} | {names}")
                    left[mid] = why
                continue
            left.pop(mid, None)
            rep["fit"] += 1
            force = mid in recheck
            tries = 0 if force else (old or {}).get("tries") or 0
            if force:
                log(f"  game {mid}: re-check asked, fetching it again")
            if old and later and not old.get("later") and not old.get("skip"):
                try:     # the game was played again after it was queued: tell the queue (and keep auto-confirm off it)
                    site_ref.collection("pending").document(mid).update({"restore.later": later})
                    old["later"] = later
                except Exception:  # noqa: BLE001 - already removed from the queue
                    old["later"] = later
            if old and not force and (old.get("stats") or tries >= MAX_TRIES or now - (old.get("tried") or 0) < tries * RETRY_GAP):
                continue                     # done, given up, or too soon to try the replay again
            if over(WORK_BUDGET):
                log(f"  game {mid}: out of time this run, next run picks it up")
                continue
            log(f"  game {mid} {s.get('map')}: downloading replay (try {tries + 1})")
            stats = None
            with tempfile.TemporaryDirectory() as tmp:
                path = download_replay(mid, [p["profileId"] for p in s["players"] if p["profileId"] in pidmap], tmp, get)
                if path:
                    try:
                        stats = core.parse_replay(path)
                    except Exception as e:  # noqa: BLE001
                        log(f"  parse failed: {e}")
            rs = restore_shift(stats) if stats else None
            game = core.merge(s, stats)
            fill_winner(game)
            if rs is not None or later:
                game["restore"] = {"later": later}
                if rs is not None:
                    game["restore"].update({"ms": stats.get("restoreMs"), "at": core.mmss(rs) if rs else None, "prev": prev,
                                            "partTime": stats.get("partTime")})
                    game["restoreMs"], game["partTime"] = stats.get("restoreMs"), stats.get("partTime")
                    log(f"  restored from a save at {core.mmss(rs) if rs else '?'}" + (f", earlier part {prev}" if prev else ""))
            if stats:
                body = json.dumps(game, separators=(",", ":"))
                if len(body) < 950000:
                    site_ref.collection("games").document(mid).set({"json": body, "matchId": mid, "started": game.get("started"), "updated": firestore.SERVER_TIMESTAMP})
                else:
                    log(f"  stats too big to store ({len(body)} bytes)")
                    stats = None
            doc = pending_doc(game, t["id"], pidmap, stats, tries + 1)
            doc["tried"] = now
            if old:   # keep the admin's decision; only add the stats/winner we now have
                upd = {k: doc[k] for k in ("players", "stats", "tries", "tried", "gameTime", "map")}
                if force:
                    if not stats and old.get("stats"):   # replay didn't come this time: keep the stats we had, fix the name only
                        upd = {"map": doc["map"]}
                    upd.update({"recheck": False, "rechecked": now})
                site_ref.collection("pending").document(mid).set(upd, merge=True)
            else:
                doc["seen"] = firestore.SERVER_TIMESTAMP
                if force:
                    doc.update({"recheck": False, "rechecked": now})
                site_ref.collection("pending").document(mid).set(doc)
                added += 1
            if force and not stats and (old or {}).get("stats"):
                stats = True                 # still have the earlier replay's stats
            known[mid] = {"stats": bool(stats), "tries": tries + 1, "tried": now, **({"later": later} if later else {})}
            log(f"  {'full stats' if stats else 'result only, will retry'}")
            if t.get("autoConfirm") and (stats or tries + 1 >= MAX_TRIES):   # wait for the replay so the stats go in too
                try:
                    log(f"  {auto_confirm(db, site_ref, t['id'], mid)}")
                except Exception as e:  # noqa: BLE001
                    log(f"  auto-confirm failed, left in the queue: {e}")
        for k in sorted(left, key=lambda x: int(x) if str(x).isdigit() else 0)[:-300]:
            del left[k]                      # keep the list small
        for w in left.values():
            rep["skipped"][w] = rep["skipped"].get(w, 0) + 1
        if deep and set(pidmap) <= set((meta.get("deepDone") or {}).get(t["id"]) or []):
            meta.setdefault("caughtUp", {})[t["id"]] = True
            (meta.get("deepDone") or {}).pop(t["id"], None)
        elif failed:
            meta.setdefault("caughtUp", {})[t["id"]] = False     # page back again next run (only the players not done yet)
    for t in state.get("tours") or []:
        if t.get("reparse"):
            try:
                reparse_tour(db, site_ref, t, meta, tour_pids(state, t), now, get)
            except Exception as e:  # noqa: BLE001 - never lose the run over this
                log(f"{site_ref.id}: re-parse failed: {e}")
    try:
        if over(WORK_BUDGET):
            raise RuntimeError("skipped, out of time this run")
        update_elos(site_ref, state, seen_matches, now, get)
    except Exception as e:  # noqa: BLE001
        log(f"{site_ref.id}: ELO update failed: {e}")
    meta["lastRun"] = firestore.SERVER_TIMESTAMP
    meta_ref.set(meta)
    return added


def active_sites(db, now):
    """Tournaments that are on (or ended in the last 3 days), found from the small search entries so finished
    tournaments cost nothing to skip."""
    from google.cloud.firestore_v1.base_query import FieldFilter
    q = db.collection("index").where(filter=FieldFilter("endTs", ">=", now - 3 * DAY))
    return [db.collection("tournaments").document(x.id) for x in q.stream()]


def main():
    import firebase_admin
    from firebase_admin import credentials, firestore
    key = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if key:
        firebase_admin.initialize_app(credentials.Certificate(json.loads(key)))
    else:
        sys.exit("FIREBASE_SERVICE_ACCOUNT is not set")
    db = firestore.client()
    total = 0
    for ref in active_sites(db, time.time()):
        try:
            total += run_site(db, ref)
        except Exception as e:  # noqa: BLE001
            log(f"{ref.id}: failed: {e}")
    log(f"done: {total} new game(s) waiting for confirmation")


if __name__ == "__main__":
    main()
