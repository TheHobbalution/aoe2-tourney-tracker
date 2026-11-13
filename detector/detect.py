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
import io, json, os, sys, tempfile, time, zipfile, urllib.request
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


def fetch_matches(pids, start_ts, deep, get=http_get):
    """Newest matches of each profile id, one entry per match id. deep=True pages back to start_ts."""
    seen, failed = {}, []
    for pid in pids:
        for page in range(1, (MAX_PAGES if deep else 1) + 1):
            try:
                data = json.loads(get(f"https://data.aoe2companion.com/api/matches?profile_ids={pid}&page={page}"))
            except Exception as e:  # noqa: BLE001
                log(f"match list for {pid} (page {page}) failed: {e}")
                failed.append(pid)
                break
            ms = data.get("matches", [])
            for m in ms:
                if m.get("matchId") is not None:
                    seen[str(m["matchId"])] = m
            oldest = min((core.iso_ts(m.get("started")) or 0 for m in ms), default=0)
            if not ms or oldest < start_ts:
                break
            time.sleep(PAUSE)
        time.sleep(PAUSE)
    return list(seen.values()), failed


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
    """Team tournaments: a real fixture is one tournament team against another. Every tournament player on a side
    must be from the same team, they must be at least half of that side, and the two sides must be different teams.
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
        if len(tms) < need or len(set(tms)) != 1 or len(tms) * 2 < len(ps):
            return False
        found.append(tms[0])
    return found[0] != found[1]


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
    }.items() if v is not None}


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


def fetch_elos(pids, get=http_get):
    """profile id -> {ladder: {...}} from each player's aoe2companion profile. Players whose profile can't be read are left out."""
    out, bad = {}, 0
    for pid in pids:
        try:
            data = json.loads(get(f"https://data.aoe2companion.com/api/profiles/{pid}"))
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


def update_elos(site_ref, state, matches_by_pid, now, get=http_get):
    """Keep tournaments/<tid>/detector/elo fresh: {at, p: {tracker player id: {ladder: {r, max}}}}."""
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
    got = fetch_elos(need, get) if need else {}
    changed = False
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
        ref.set({"at": cur.get("at") or 0 if fresh else now, "p": have, "asked": sorted(asked)})
        log(f"{site_ref.id}: ELOs saved for {len(have)} players ({len(got)} of {len(need)} profiles read)")


# ---------- one site ----------
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
        for d in site_ref.collection("pending").where(filter=FieldFilter("status", "==", "new")).stream():
            pd = d.to_dict() or {}
            if pd.get("tour") == t["id"] and (ranked(pd.get("leaderboard")) or not sides_fit(pd.get("players") or [], teamof)):
                d.reference.delete()
                site_ref.collection("games").document(d.id).delete()
                known[d.id] = {"stats": True, "tries": MAX_TRIES, "tried": now, "skip": True}
                log(f"  game {d.id}: ranked or not team v team, removed from the queue")
        matches, failed = fetch_matches(sorted(pidmap), window[0], deep, get)
        seen_matches += matches
        rep = {"at": now, "players": len(pidmap), "looked": len(matches), "fit": 0, "skipped": {}, "failed": len(failed)}
        meta.setdefault("report", {})[t["id"]] = rep
        left = meta.setdefault("left", {}).setdefault(t["id"], {})
        for m in matches:
            s = core.summarize_match(m)
            mid = str(s["matchId"])
            old = known.get(mid)
            why = why_not(s, pidmap, window, teamof)
            if why:
                if why not in ("few", "dates"):      # a game between tournament players inside the dates: say why it was left out
                    if left.get(mid) != why:
                        names = " v ".join(", ".join(str(p.get("name") or "?") for p in s["players"] if p.get("team") == tm) for tm in sorted({p.get("team") for p in s["players"]}, key=str))
                        log(f"  game {mid} left out ({why}): {s.get('leaderboard') or 'no label'} | {names}")
                    left[mid] = why
                continue
            left.pop(mid, None)
            rep["fit"] += 1
            tries = (old or {}).get("tries") or 0
            if old and (old.get("stats") or tries >= MAX_TRIES or now - (old.get("tried") or 0) < tries * RETRY_GAP):
                continue                     # done, given up, or too soon to try the replay again
            log(f"  game {mid} {s.get('map')}: downloading replay (try {tries + 1})")
            stats = None
            with tempfile.TemporaryDirectory() as tmp:
                path = download_replay(mid, [p["profileId"] for p in s["players"] if p["profileId"] in pidmap], tmp, get)
                if path:
                    try:
                        stats = core.parse_replay(path)
                    except Exception as e:  # noqa: BLE001
                        log(f"  parse failed: {e}")
            game = core.merge(s, stats)
            fill_winner(game)
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
                site_ref.collection("pending").document(mid).set({k: doc[k] for k in ("players", "stats", "tries", "tried", "gameTime", "map")}, merge=True)
            else:
                doc["seen"] = firestore.SERVER_TIMESTAMP
                site_ref.collection("pending").document(mid).set(doc)
                added += 1
            known[mid] = {"stats": bool(stats), "tries": tries + 1, "tried": now}
            log(f"  {'full stats' if stats else 'result only, will retry'}")
        for k in sorted(left, key=lambda x: int(x) if str(x).isdigit() else 0)[:-300]:
            del left[k]                      # keep the list small
        for w in left.values():
            rep["skipped"][w] = rep["skipped"].get(w, 0) + 1
        if deep and not failed:
            meta.setdefault("caughtUp", {})[t["id"]] = True
        elif failed:
            meta.setdefault("caughtUp", {})[t["id"]] = False     # page back again next run so nothing is missed
    try:
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
