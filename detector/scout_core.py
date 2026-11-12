"""Replay + match helpers lifted unchanged from Jacob's HOBB LAN lan_scout.py (functions only).
Generated file: edit lan_scout and re-extract rather than editing here."""
import io, json, os, re, sys, zipfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
NAMES = json.load(open(os.path.join(HERE, "aoe_names.json"), encoding="utf-8"))
PID_TO_P = {}   # not used by the tourney detector (the tracker maps profile ids itself)
def log(*a):
    print(*a, flush=True)

try:
    import aoe2_replay_stats as S
except Exception as e:  # noqa: BLE001
    S = None
    log("analyser not available, basic stats only:", e)
try:
    import eapm_detail as ED
except Exception as e:  # noqa: BLE001
    ED = None
    log("eAPM detail not available:", e)

AGES = {101: "Feudal", 102: "Castle", 103: "Imperial"}
ECO = {22, 213, 249, 14, 13, 12, 202, 203, 221, 55, 278, 279, 182, 8, 280}  # loom, wheelbarrow, hand cart, farm/wood/mining/town ups
SKIP = {"SPECTATE", "SAVE", "CHAPTER", "AI_ORDER", "AI_COMMAND", "DE_AUTOSCOUT"}
VILS = {83, 293}

def iso_ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def summarize_match(m):
    """aoe2companion match -> our summary (civs, teams, winner)."""
    players = []
    for t in m.get("teams", []):
        for p in t.get("players", []):
            pid = p.get("profileId")
            players.append({
                "profileId": pid, "p": PID_TO_P.get(pid), "name": p.get("name"),
                "team": p.get("team", t.get("teamId")), "civ": p.get("civName") or p.get("civ"),
                "won": p.get("won"), "color": p.get("color"),
            })
    return {
        "matchId": m.get("matchId"), "started": iso_ts(m.get("started")), "finished": iso_ts(m.get("finished")),
        "map": m.get("mapName"), "leaderboard": m.get("leaderboardName") or m.get("leaderboardId"),
        "players": players,
    }


def obj(i):
    return NAMES["objects"].get(str(i), f"#{i}")


def tech(i):
    return NAMES["techs"].get(str(i), f"#{i}")


def mmss(sec):
    sec = int(round(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def real_map(path):
    """The map actually played. With a HyperRandom map pool, rms_filename is a pool entry, not the map the
    game picked (6 Nov: 'HyperRandomWater' was really LightLand); the real one is in the instructions text
    as 'Location: ZN@HyperRandomLightLand'. For any other map (no pool) that text can be stale from an
    earlier lobby (NUB FourLakes showed 'Location: ...Extreme'), so rms_filename wins there."""
    from mgz.fast.header import decompress, parse
    try:
        with open(path, "rb") as f:
            rms = parse(f)["de"].get("rms_filename") or ""
        base = os.path.splitext(rms)[0]
        if "hyperrandom" not in base.lower():
            return base or None
        with open(path, "rb") as f:
            m = re.search(rb"\nLocation: ([^\r\n]+)", decompress(f).read())
        loc = m.group(1).decode("utf-8", "replace").strip() if m else ""
        return loc if "hyperrandom" in loc.lower() else (base or None)
    except Exception:  # noqa: BLE001
        return None


def parse_replay(path):
    from mgz.fast import operation, meta
    from mgz.fast.header import parse
    from mgz.fast.enums import Operation, Action
    with open(path, "rb") as f:
        eof = os.fstat(f.fileno()).st_size
        h = parse(f)
        de = h["de"]
        speed = de.get("speed") or 1.7
        num = {}  # player number -> info
        for dp in de["players"]:
            pid = dp.get("profile_id")
            if not pid or pid == 4294967295 or dp.get("number", 0) <= 0:
                continue
            name = dp["name"].decode("utf-8", "replace") if isinstance(dp["name"], bytes) else dp["name"]
            num[dp["number"]] = {
                "profileId": pid, "p": PID_TO_P.get(pid), "name": name, "team": dp.get("team_id"),
                "color": dp.get("color_id"),   # 0 blue, 1 red, 2 green, 3 yellow, 4 teal, 5 purple, 6 grey, 7 orange
                "civ": NAMES["civs"].get(str(dp.get("civilization_id")), str(dp.get("civilization_id"))),
                "actions": 0, "ages": {}, "build": [], "techs": [], "units": {}, "vils": 0, "military": 0,
                "eco": [], "resigned": None, "walls": 0,
            }
        meta(f)
        ms = 0
        while f.tell() < eof:
            try:
                t, pl = operation(f)
            except EOFError:
                break
            except Exception:
                break
            if t == Operation.SYNC:
                ms += pl[0]
                continue
            if t != Operation.ACTION:
                continue
            a, d = pl
            p = num.get(d.get("player_id"))
            if not p or a.name in SKIP:
                continue
            sec = ms / 1000
            p["actions"] += 1
            if a == Action.RESEARCH:
                tid = d.get("technology_id")
                if tid in AGES and AGES[tid] not in p["ages"]:
                    p["ages"][AGES[tid]] = mmss(sec)
                elif tid not in AGES and tech(tid) not in (t[1] for t in p["techs"]):
                    p["techs"].append([mmss(sec), tech(tid)])
                    if tid in ECO:
                        p["eco"].append([mmss(sec), tech(tid)])
            elif a == Action.BUILD and len(p["build"]) < 40:
                p["build"].append([mmss(sec), obj(d.get("building_id"))])
            elif a in (Action.DE_QUEUE, Action.QUEUE, Action.MAKE):
                uid, n = d.get("unit_id"), d.get("amount", 1) or 1
                if uid is not None:
                    nm = obj(uid)
                    p["units"][nm] = p["units"].get(nm, 0) + n
                    if uid in VILS:
                        p["vils"] += n
                    else:
                        p["military"] += n
            elif a == Action.WALL:
                p["walls"] += 1
            elif a == Action.RESIGN and p["resigned"] is None:
                p["resigned"] = mmss(sec)
    game_sec = ms / 1000
    rms = de.get("rms_filename")
    map_name = os.path.splitext(rms)[0] if rms else NAMES["maps"].get(str(de.get("rms_map_id")), "")
    map_name = real_map(path) or map_name
    for p in num.values():
        mins = max(game_sec / 60, 0.1)
        p["apm"] = round(p["actions"] / mins * speed)   # per real minute, like most stat sites
    if S is not None:
        try:
            helper_stats(path, num)
        except Exception as e:  # noqa: BLE001
            log(f"  Desk Scout analyser failed, basic stats only: {e}")
    if ED is not None:
        try:
            det = ED.detail(path)
            for p in num.values():
                if p["profileId"] in det:
                    p["detail"] = det[p["profileId"]]
        except Exception as e:  # noqa: BLE001
            log(f"  eAPM detail failed: {e}")
    return {
        "guid": de.get("guid"), "started": de.get("timestamp"), "map": map_name, "rated": de.get("rated"),
        "gameTime": mmss(game_sec), "realTime": mmss(game_sec / speed), "players": list(num.values()),
    }


def _split(line):
    t, _, rest = str(line).partition(" ")
    return [t, rest]


def helper_stats(path, num):
    """Swap in the Desk Scout analyser's numbers (same as the board's post-game pages)."""
    res = S.analyse(S.open_replay(path))
    by_pid = {r.get("profile_id"): r for r in res["players"]}
    for p in num.values():
        r = by_pid.get(p["profileId"])
        if not r:
            continue
        idle = r.get("tc_idle_estimate") or {}
        bank = r.get("banked_resources_experimental") or {}
        units = dict(r.get("units") or {})
        p.update({
            "apm": r.get("apm"), "eapm": r.get("eapm"), "opening": r.get("opening"),
            "ages": {a: d.get("clicked") for a, d in (r.get("ages") or {}).items()},
            "agesDone": {a: d.get("done_est") for a, d in (r.get("ages") or {}).items()},
            "vils": r.get("villagers", 0), "military": sum(units.values()),
            "units": dict(units, **({"Villager": r.get("villagers")} if r.get("villagers") else {})),
            "techs": [_split(x) for x in r.get("techs") or []],
            "build": [_split(x) for x in r.get("build_order") or []],
            "eco": [[mmss(sec), S.tech_name(tid)] for tid, sec in sorted((r.get("eco_ups") or {}).items(), key=lambda kv: kv[1])],
            "tcIdle": {k[:-3]: round((v or 0) / 1000) for k, v in idle.items() if k.endswith("_ms") and v is not None},
            "floatAvg": bank.get("avg_total"), "floatPeak": bank.get("peak"),
            "spent": r.get("spent_estimate"), "resigned": r.get("resigned_at") or p["resigned"],
            "result": r.get("result"),
        })


def merge(summary, stats):
    """Combine aoe2companion summary (winner, civs) with replay stats."""
    g = dict(summary or {})
    if stats:
        g.update({k: stats[k] for k in ("guid", "gameTime", "realTime", "rated")})
        g["map"] = stats["map"] or g.get("map")
        g.setdefault("started", stats["started"])
        by_pid = {p["profileId"]: p for p in stats["players"]}
        if not g.get("players"):
            g["players"] = [{k: p.get(k) for k in ("profileId", "p", "name", "team", "civ", "color")} for p in stats["players"]]
            # winner from resignations: team(s) with nobody resigned
            # team 1 = "-" (no team): each such player is on their own side
            # ...but if other players have real team ids, team 1 is a real team too (e.g. 2v2 as teams 1 and 2, 5 Nov)
            t1_team = sum(1 for q in stats["players"] if q["team"] == 1) > 1 and any((q["team"] or 0) > 1 for q in stats["players"])
            side = lambda p: p["team"] if (p["team"] or 0) > 1 or (p["team"] == 1 and t1_team) else 100 + (p["p"] if p["p"] is not None else p["profileId"])
            res = {side(p) for p in stats["players"] if p["resigned"]}
            for p, sp in zip(g["players"], stats["players"]):
                if sp.get("result") in ("WIN", "LOSS"):
                    p["won"] = sp["result"] == "WIN"
                else:
                    p["won"] = (side(sp) not in res) if res else None
        for p in g["players"]:
            s = by_pid.get(p["profileId"])
            if s:
                p["stats"] = {k: s[k] for k in ("apm", "eapm", "opening", "ages", "agesDone", "build", "techs", "eco", "units",
                                                "vils", "military", "walls", "resigned", "tcIdle", "floatAvg", "floatPeak", "spent", "detail") if k in s}
                p["civ"] = p.get("civ") or s["civ"]
                if s.get("color") is not None:
                    p["color"] = s["color"]     # colour actually played, from the replay
    return g

