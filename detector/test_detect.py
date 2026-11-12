"""Detector test: fake aoe2companion + aoe.ms, real Firestore emulator. Run inside `firebase emulators:exec`."""
import json, os, sys, time
os.environ.setdefault("GCLOUD_PROJECT", "demo-tt")
import firebase_admin
from firebase_admin import firestore
import detect

fails = 0
def ok(c, m):
    global fails
    print(("PASS " if c else "FAIL ") + m)
    fails += 0 if c else 1

NOW = detect.day_ts("2026-11-20")
ISO = lambda ts: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))

def match(mid, started, teams, finished=True, won=(True, False)):
    return {"matchId": mid, "started": ISO(started), "finished": ISO(started + 1800) if finished else None, "mapName": "Arabia", "leaderboardName": "Unranked",
            "teams": [{"teamId": i + 1, "players": [{"profileId": p, "name": f"n{p}", "civName": "Franks", "won": won[i], "color": 1} for p in t]} for i, t in enumerate(teams)]}

# 8 tournament players (pids 101-108), two teams of 4 (+ a sub with pid 109 on team A); player x has no pid
players = {f"p{i}": {"id": f"p{i}", "name": f"P{i}", "pid": str(100 + i)} for i in range(1, 10)}
players["px"] = {"id": "px", "name": "NoPid", "pid": ""}
tour = {"id": "t1", "name": "AusDraft 5", "start": "2026-11-16", "end": "2026-12-20", "roster": [],
        "teams": [{"id": "A", "name": "A", "members": ["p1", "p2", "p3", "p4", "p9"]}, {"id": "B", "name": "B", "members": ["p5", "p6", "p7", "p8", "px"]}]}
old_tour = {"id": "t0", "name": "Old", "start": "2026-01-01", "end": "2026-01-05", "roster": ["p1", "p2"], "teams": []}
state = {"app": "tourney-tracker", "v": 1, "players": players, "porder": list(players), "tours": [old_tour, tour], "cur": "t1"}

# pure functions
ok(detect.tour_window(old_tour, NOW) is None, "finished tournament is skipped")
ok(detect.tour_window(tour, NOW) is not None, "running tournament is picked up")
pm = detect.tour_pids(state, tour)
ok(len(pm) == 9 and pm[101] == "p1" and pm[109] == "p9", "profile ids mapped from team members (no-pid player left out)")
W = detect.tour_window(tour, NOW)
import scout_core as core
T0 = detect.day_ts("2026-11-18")
ok(detect.is_candidate(core.summarize_match(match(1, T0, [[101, 102, 103, 104], [105, 106, 107, 108]])), pm, W), "4v4 between rosters is a candidate")
ok(not detect.is_candidate(core.summarize_match(match(2, T0, [[101, 102, 901, 902], [903, 904, 905, 906]])), pm, W), "ranked game with mostly strangers is not")
ok(not detect.is_candidate(core.summarize_match(match(3, T0, [[101, 102, 103, 104], [105, 106, 107, 108]], finished=False)), pm, W), "unfinished game is not")
ok(not detect.is_candidate(core.summarize_match(match(4, detect.day_ts("2026-10-01"), [[101, 102, 103, 104], [105, 106, 107, 108]])), pm, W), "game before the tournament is not")
g = {"players": [{"team": 1, "won": None, "stats": {"resigned": "30:00"}}, {"team": 1, "won": None, "stats": {"resigned": "31:00"}}, {"team": 2, "won": None, "stats": {}}, {"team": 2, "won": None, "stats": {"resigned": "20:00"}}]}
detect.fill_winner(g); ok([p["won"] for p in g["players"]] == [False, False, True, True], "winner worked out from resignations when missing")
g = {"players": [{"team": 1, "won": None, "stats": {"result": "WIN"}}, {"team": 2, "won": None, "stats": {"result": "LOSS"}}]}
detect.fill_winner(g); ok([p["won"] for p in g["players"]] == [True, False], "winner taken from replay result when present")
g = {"players": [{"team": 1, "won": None, "stats": {}}, {"team": 2, "won": None, "stats": {}}]}
detect.fill_winner(g); ok(g["players"][0]["won"] is None, "winner left blank when unknown (admin picks)")

# end to end against the emulator
from google.cloud import firestore as gfs
from google.auth.credentials import AnonymousCredentials
db = gfs.Client(project="demo-tt", credentials=AnonymousCredentials())
site = db.collection("tournaments").document("dtest")
site.set({"state": json.dumps(state), "admins": ["x"]})
calls = {"n": 0, "replay": 0}
feed = {101: [match(10, T0, [[101, 102, 103, 104], [105, 106, 107, 108]]), match(11, T0 + 3600, [[101, 102, 103, 109], [105, 106, 107, 108]], won=(None, None)), match(12, T0, [[101, 901], [902, 903]])],
        105: [match(10, T0, [[101, 102, 103, 104], [105, 106, 107, 108]])]}
def fake_get(url, timeout=30):
    if "aoe2companion" in url:
        calls["n"] += 1
        pid = int(url.split("profile_ids=")[1].split("&")[0]); page = int(url.split("page=")[1])
        return json.dumps({"matches": feed.get(pid, []) if page == 1 else []}).encode()
    calls["replay"] += 1
    raise OSError("no replay in test")
detect.PAUSE = 0
n = detect.run_site(db, site, now=NOW, get=fake_get)
pend = {d.id: d.to_dict() for d in site.collection("pending").stream()}
ok(n == 2 and set(pend) == {"10", "11"}, f"two games queued, stranger game ignored ({sorted(pend)})")
p10 = pend.get("10", {})
ok(p10.get("status") == "new" and p10.get("tour") == "t1" and p10.get("map") == "Arabia" and p10.get("stats") is False, "queue entry has status, tournament, map")
ok([p["id"] for p in p10.get("players", [])][:2] == ["p1", "p2"] and p10["players"][0]["won"] is True and p10["players"][4]["won"] is False, "players mapped to tracker ids with winner")
ok(pend.get("11", {}).get("players", [{}])[0].get("won") is None, "missing winner stays blank")
ok((site.collection("detector").document("meta").get().to_dict() or {}).get("caughtUp", {}).get("t1") is True, "catch-up marked done")
c1 = calls["n"]
# admin confirms game 10; next run must not reset it, and must retry the replay only up to the limit
site.collection("pending").document("10").update({"status": "confirmed"})
n2 = detect.run_site(db, site, now=NOW + 60, get=fake_get)
ok({d.id: d.to_dict() for d in site.collection("pending").stream()}["11"]["tries"] == 1, "no replay retry within 15 minutes")
for i in range(1, 12):
    n2 = detect.run_site(db, site, now=NOW + i * 7200, get=fake_get)
pend = {d.id: d.to_dict() for d in site.collection("pending").stream()}
ok(n2 == 0 and pend["10"]["status"] == "confirmed", "confirmed game keeps its status on later runs")
ok(pend["10"]["tries"] == detect.MAX_TRIES and pend["11"]["tries"] == detect.MAX_TRIES, f"replay retried {detect.MAX_TRIES} times then left alone")
ok((calls["n"] - c1) == 12 * 9, f"later runs read one page per player ({(calls['n'] - c1) // 12} calls per run)")
ok(len(list(site.collection("games").stream())) == 0, "no stats stored when no replay")
# which tournaments get checked: only those that are on or just ended
idx = db.collection("index")
idx.document("dtest").set({"endTs": NOW + 5 * 86400})
idx.document("dold").set({"endTs": NOW - 30 * 86400})
idx.document("drecent").set({"endTs": NOW - 1 * 86400})
ids = sorted(r.id for r in detect.active_sites(db, NOW))
ok(ids == ["drecent", "dtest"], f"only running or just-ended tournaments are checked ({ids})")
print("\nALL PASSED" if not fails else f"\n{fails} FAILED"); sys.exit(1 if fails else 0)
