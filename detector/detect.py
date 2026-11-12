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


def is_candidate(summary, pidmap, window):
    """Finished, inside the dates, 2+ tournament players and they are at least half the lobby."""
    ps = summary.get("players") or []
    ours = [p for p in ps if p.get("profileId") in pidmap]
    st = summary.get("started") or 0
    return bool(summary.get("finished")) and window[0] <= st <= window[1] and len(ours) >= 2 and len(ours) * 2 >= len(ps)


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
    return {k: v for k, v in {
        "eapm": st.get("eapm"), "apm": st.get("apm"), "opening": st.get("opening"),
        "feudal": ages.get("Feudal"), "castle": ages.get("Castle"), "imp": ages.get("Imperial"),
        "vils": st.get("vils"), "mil": st.get("military"),
        "tcIdle": sum((st.get("tcIdle") or {}).values()) if st.get("tcIdle") else None,
    }.items() if v is not None}


# ---------- one site ----------
def run_site(db, site_ref, now=None, get=http_get):
    from firebase_admin import firestore
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
    for t in state.get("tours") or []:
        window = tour_window(t, now)
        pidmap = tour_pids(state, t)
        if not window or len(pidmap) < 2:
            continue
        deep = not (meta.get("caughtUp") or {}).get(t["id"])
        log(f"{site_ref.id} / {t.get('name')}: {len(pidmap)} players with profile IDs{' (catch-up)' if deep else ''}")
        matches, failed = fetch_matches(sorted(pidmap), window[0], deep, get)
        for m in matches:
            s = core.summarize_match(m)
            mid = str(s["matchId"])
            old = known.get(mid)
            if not is_candidate(s, pidmap, window):
                continue
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
        if deep and not failed:
            meta.setdefault("caughtUp", {})[t["id"]] = True
        elif failed:
            meta.setdefault("caughtUp", {})[t["id"]] = False     # page back again next run so nothing is missed
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
