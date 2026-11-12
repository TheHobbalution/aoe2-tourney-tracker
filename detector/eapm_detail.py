"""eAPM detail for the LAN Scout, in the style of AoE2 Insights' eAPM panel.

Per player: eAPM for each game minute, average and peak, military vs eco split, focus time and micro vs macro.
The replay only says which unit ids a command moves, not what they are, so the military/eco side of a
move or right-click is an estimate: ids seen building, repairing or sent to the same targets as known
villagers count as villagers, everything else as army. Production, research and building have a fixed side.
"""
import os
import struct
import mgz.fast as _mf
from mgz.fast import operation, meta
from mgz.fast.header import parse
from mgz.fast.enums import Operation, Action

# mgz-fast reads MOVE/ORDER unit ids from the wrong offset in current DE replays (the ids start 20 bytes
# into the command, after the selected count and 7 more bytes). Re-read them; chains onto any earlier patch.
_prev = _mf.parse_action


def _fixed(action_type, data):
    out = _prev(action_type, data)
    if action_type in (Action.MOVE, Action.ORDER):
        try:
            body = data[3:]
            n = body[12]
            ids = list(struct.unpack_from(f"<{n}I", body, 20)) if 0 < n < 255 else []
            out = dict(out, object_ids=ids)
        except struct.error:
            pass
    return out


_mf.parse_action = _fixed

NOT_PLAYER = {"GAME", "SAVE", "CHAPTER", "AI_ORDER", "AI_COMMAND", "SPECTATE", "POSTGAME", "ERROR",
              "DE_AUTOSCOUT", "DE_TRANSFORM"}
VILS = {83, 293}
AGES = {101, 102, 103}
ECO_TECHS = {22, 213, 249, 14, 13, 12, 202, 203, 221, 55, 278, 279, 182, 8, 280, 19, 23, 17, 15}
ECO_CMDS = {"BUILD", "WALL", "REPAIR", "SELL", "BUY", "TRIBUTE",
            "DE_TRIBUTE", "BACK_TO_WORK", "TOWN_BELL", "GATE"}
MIL_CMDS = {"STANCE", "GUARD", "FOLLOW", "PATROL", "FORMATION", "DE_ATTACK_MOVE", "DE_RETREAT",
            "ATTACK_GROUND", "RATHA_ABILITY", "DROP_RELIC"}
MAKE_CMDS = {"MAKE", "QUEUE", "DE_QUEUE", "MULTIQUEUE"}
NEUTRAL = {"FLARE", "DELETE", "GAME", "GATHER_POINT", "DE_MULTI_GATHERPOINT"}  # rally points: TC or barracks, can't tell


def _side(name, d, st):
    """Return (side, kind): side 'mil'/'eco'/None, kind 'micro'/'macro'."""
    if name in ECO_CMDS:
        ids = d.get("object_ids") or []
        if name in ("BUILD", "WALL", "REPAIR"):
            st["vils"].update(ids)
            if d.get("target_id") not in (None, -1):
                st["eco_targets"].add(d.get("target_id"))
        return "eco", "macro"
    if name in MAKE_CMDS:
        if d.get("unit_id") in VILS:
            return "eco", "macro"
        st["army_started"] = True
        return "mil", "macro"
    if name == "RESEARCH":
        t = d.get("technology_id")
        return ("eco" if t in AGES or t in ECO_TECHS else "mil"), "macro"
    if name in MIL_CMDS:
        return "mil", "micro"
    if name in NEUTRAL:
        return None, "macro"
    # unit control: MOVE, ORDER, STOP, WORK, waypoints, ungarrison, special...
    ids = set(d.get("object_ids") or [])
    tgt = d.get("target_id")
    vil = bool(ids & st["vils"]) or (tgt not in (None, -1) and tgt in st["eco_targets"])
    if not vil and not ids and not st["army_started"]:
        vil = True                       # Dark Age with no ids read: almost always villagers
    if not vil and ids and not st["army_started"] and len(ids) > 1:
        vil = True                       # groups before any army is made are villagers
    if vil:
        if not st["army_started"]:
            st["vils"].update(ids)       # later selections may mix villagers and army, so don't learn from them
        if tgt not in (None, -1):
            st["eco_targets"].add(tgt)
        return "eco", "macro"
    return "mil", "micro"


def detail(path):
    """Return {profile_id: detail dict}."""
    with open(path, "rb") as f:
        eof = os.fstat(f.fileno()).st_size
        h = parse(f)
        num = {}
        for dp in h["de"]["players"]:
            if dp.get("number", 0) > 0 and dp.get("profile_id") not in (None, 4294967295):
                num[dp["number"]] = {"pid": dp["profile_id"], "ev": [], "last": None,
                                     "st": {"vils": set(), "eco_targets": set(), "army_started": False}}
        meta(f)
        ms = 0
        while f.tell() < eof:
            try:
                t, pl = operation(f)
            except Exception:  # noqa: BLE001
                break
            if t == Operation.SYNC:
                ms += pl[0]
                continue
            if t != Operation.ACTION:
                continue
            a, d = pl
            p = num.get(d.get("player_id"))
            if not p or a.name in NOT_PLAYER:
                continue
            key = (a.name, tuple(d.get("object_ids") or ()), d.get("target_id"), round(d.get("x") or 0),
                   round(d.get("y") or 0), d.get("unit_id"), d.get("technology_id"))
            if p["last"] and key == p["last"][0] and ms - p["last"][1] < 500:
                p["last"] = (key, ms)
                continue                 # spam / double click: not effective
            p["last"] = (key, ms)
            side, kind = _side(a.name, d, p["st"])
            p["ev"].append((ms, side, kind))
    out = {}
    n_min = max(1, int(ms // 60000) + (1 if ms % 60000 >= 30000 else 0))
    for p in num.values():
        tot, mil, eco, mic, mac = ([0] * n_min for _ in range(5))
        for t, side, kind in p["ev"]:
            i = min(int(t // 60000), n_min - 1)
            tot[i] += 1
            if side == "mil":
                mil[i] += 1
            elif side == "eco":
                eco[i] += 1
            if kind == "micro":
                mic[i] += 1
            else:
                mac[i] += 1
        mins = max(ms / 60000, 0.01)
        mil_s = eco_s = 0.0
        for i in range(n_min):
            span = min(60.0, ms / 1000 - i * 60)
            if mil[i] + eco[i]:
                mil_s += span * mil[i] / (mil[i] + eco[i])
                eco_s += span * eco[i] / (mil[i] + eco[i])
        both = max(mil_s + eco_s, 1)
        out[p["pid"]] = {
            "eapmSeries": tot, "milSeries": mil, "ecoSeries": eco,
            "microSeries": [round((mic[i] - mac[i]) / tot[i], 2) if tot[i] else 0 for i in range(n_min)],
            "eapmAvg": round(sum(tot) / mins), "eapmPeak": max(tot),
            "milAvg": round(sum(mil) / mins), "milPeak": max(mil),
            "ecoAvg": round(sum(eco) / mins), "ecoPeak": max(eco),
            "focusMil": round(mil_s), "focusEco": round(eco_s),
            "focusMilPct": round(100 * mil_s / both), "focusEcoPct": round(100 * eco_s / both),
            "microPct": round(100 * sum(mic) / max(sum(tot), 1)),
        }
    return out


if __name__ == "__main__":
    import json
    import sys
    r = detail(sys.argv[1])
    for pid, v in r.items():
        print(pid, {k: v[k] for k in v if not k.endswith("Series")})
