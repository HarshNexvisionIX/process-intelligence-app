"""Work episodes (sittings): stretches of one person's work, never cases -> nc_episodes.parquet + episode_id.

    SourceSession     session_id mapped          the source's own session
    ActorGap          actor + time               a new episode after nocase.episodes.gap_minutes idle
    ActorAppContext   actor + app + screen       ... or when, after a pause over context_minutes, the actor
                                                 moves to an app / screen not yet seen in the current episode
    DayWindow         always                     one episode per actor (or app, without an actor) per day

Episode IDs (E000001 ...) follow the episodes' start time, then their first event: the same on every run.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from app.core.db import sql_literal
from app.nocase.strategies.base import Context, Strategy, cfg, register

EPISODES_FILE = "nc_episodes.parquet"


def _ecfg() -> dict:
    return cfg()["episodes"]


def _fill(caps: dict, col: str) -> int:
    return int(((caps.get("fill") or {}).get(col) or {}).get("rows") or 0)


def load_events(con, ctx: Context) -> pd.DataFrame:
    return con.execute(f"""SELECT eid, ts, actor, app, screen_clean, unit_id, active_s, l3, session_id
                           FROM {ctx.events_sql()} ORDER BY actor NULLS LAST, ts, eid""").df()


def _number(keys: pd.DataFrame) -> pd.Series:
    """episode key per event -> E000001 ... by (start, first eid)."""
    first = keys.groupby("key").agg(start=("ts", "min"), eid=("eid", "min")).sort_values(["start", "eid"])
    ids = pd.Series([f"E{i:06d}" for i in range(1, len(first) + 1)], index=first.index)
    return keys["key"].map(ids)


class _EpisodeStrategy(Strategy):
    stage = "episodes"

    def keys(self, ev: pd.DataFrame) -> pd.Series:      # one key per event (any hashable)
        raise NotImplementedError

    def compute(self, con, ctx: Context) -> pd.DataFrame:
        """eid, episode_id (without writing anything): used by run() and by the evaluation."""
        ev = load_events(con, ctx)
        ev["key"] = self.keys(ev).values
        ev["episode_id"] = _number(ev[["key", "ts", "eid"]])
        return ev

    def run(self, con, ctx: Context) -> str:
        ev = self.compute(con, ctx)
        summary = ev.groupby("episode_id").agg(
            actor=("actor", "first"), start=("ts", "min"), end=("ts", "max"), events=("eid", "size"),
            active_s=("active_s", "sum"), apps=("app", "nunique"), units=("unit_id", "nunique"),
            l3s=("l3", "nunique")).reset_index()
        summary["strategy"] = self.name
        summary.to_parquet(ctx.path(EPISODES_FILE), index=False)
        attach(con, ctx, ev[["eid", "episode_id"]])
        return EPISODES_FILE

    def quality(self, con, ctx: Context) -> dict:
        e = pd.read_parquet(ctx.path(EPISODES_FILE))
        dur = (e["end"] - e["start"]).dt.total_seconds()
        return {"episodes": int(len(e)), "median_events": float(e["events"].median()) if len(e) else 0.0,
                "median_duration_s": float(dur.median()) if len(e) else 0.0,
                "single_event_share": round(float((e["events"] == 1).mean()), 4) if len(e) else 0.0}


def _gaps(ev: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(new actor stream?, seconds since the actor's previous event) per event (events sorted by actor, ts)."""
    actor = ev["actor"].fillna("").to_numpy()
    new_actor = np.r_[True, actor[1:] != actor[:-1]]
    ts = (ev["ts"] - ev["ts"].min()).dt.total_seconds().to_numpy()      # seconds, whatever the time unit
    gap = np.r_[np.inf, np.diff(ts)]
    gap[new_actor] = np.inf
    return new_actor, gap


@register
class SourceSession(_EpisodeStrategy):
    name = "SourceSession"

    def applies(self, caps, ctx):
        return (_fill(caps, "session_id") > 0,
                "the export has a session ID" if _fill(caps, "session_id") else "no session_id column is mapped")

    def keys(self, ev):
        return ev["session_id"].fillna("") + "|" + ev["actor"].fillna("")


@register
class ActorGap(_EpisodeStrategy):
    name = "ActorGap"

    def applies(self, caps, ctx):
        ok = _fill(caps, "actor") > 0 and _fill(caps, "ts") > 0
        return ok, (f"actor and time: a new episode after {_ecfg()['gap_minutes']} minutes idle" if ok
                    else "needs an actor column")

    def keys(self, ev):
        new_actor, gap = _gaps(ev)
        return pd.Series(np.cumsum(new_actor | (gap > float(_ecfg()["gap_minutes"]) * 60)), index=ev.index)


@register
class ActorAppContext(_EpisodeStrategy):
    name = "ActorAppContext"

    def applies(self, caps, ctx):
        from app.core.db import connect
        screens = connect().execute(f"SELECT COUNT(screen_clean) FROM {ctx.events_sql()}").fetchone()[0]
        ok = _fill(caps, "actor") > 0 and (_fill(caps, "app") > 0 or screens > 0)
        return ok, (f"actor with app or screen: a new episode after {_ecfg()['gap_minutes']} minutes idle, or on a "
                    f"new app / screen after a pause over {_ecfg()['context_minutes']} minutes" if ok
                    else "needs an actor and an app or screen column")

    def keys(self, ev):
        new_actor, gap = _gaps(ev)
        idle = float(_ecfg()["gap_minutes"]) * 60
        pause = float(_ecfg()["context_minutes"]) * 60
        ctxs = (ev["app"].fillna("") + "|" + ev["screen_clean"].fillna("")).to_numpy()
        out = np.empty(len(ev), dtype=np.int64)
        n, seen = 0, set()
        for i in range(len(ev)):
            if new_actor[i] or gap[i] > idle or (gap[i] > pause and ctxs[i] not in seen):
                n += 1
                seen = set()
            seen.add(ctxs[i])
            out[i] = n
        return pd.Series(out, index=ev.index)


@register
class DayWindow(_EpisodeStrategy):
    name = "DayWindow"

    def applies(self, caps, ctx):
        return True, "always possible: one episode per actor (or application) per day"

    def keys(self, ev):
        who = ev["actor"] if ev["actor"].notna().any() else ev["app"]
        return who.fillna("") + "|" + ev["ts"].dt.strftime("%Y-%m-%d")


def attach(con, ctx: Context, df: pd.DataFrame) -> None:
    """Write df's columns (keyed by eid) onto nc_events_clean.parquet, replacing columns of the same name."""
    from app.core.db import connect
    from app.nocase.clean import CLEAN_FILE
    path = ctx.path(CLEAN_FILE)
    cols = [c for c in df.columns if c != "eid"]
    con = connect()                       # its own connection, closed before the file is swapped
    con.register("__nc_attach", df)
    existing = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {ctx.events_sql()}").fetchall()]
    keep = [c for c in existing if c not in cols]
    tmp = path.with_name(path.name + ".tmp")
    con.execute(f"""COPY (SELECT {', '.join('e.' + c for c in keep)}, {', '.join('a.' + c for c in cols)}
                          FROM {ctx.events_sql()} e LEFT JOIN __nc_attach a USING (eid) ORDER BY e.eid)
                    TO {sql_literal(tmp.as_posix())} (FORMAT parquet, COMPRESSION zstd)""")
    con.close()
    os.replace(tmp, path)


def data_score(ev: pd.DataFrame) -> dict:
    """A quality score from the data only (never truth): few single-event episodes, a median length inside
    nocase.episodes.good_minutes, and few units per episode (work on one thing). 0..1, higher is better."""
    c = _ecfg()
    lo, hi = (float(x) for x in c["good_minutes"])
    w = c["score_weights"]
    g = ev.groupby("episode_id")
    size = g.size()
    single = float((size == 1).mean()) if len(size) else 1.0
    minutes = ((g["ts"].max() - g["ts"].min()).dt.total_seconds() / 60)
    med = float(minutes.median()) if len(minutes) else 0.0
    length = 1.0 if lo <= med <= hi else (med / lo if med < lo else hi / med)
    top = ev.groupby(["episode_id", "unit_id"]).size().groupby(level=0).max()
    concentration = float((top / size).mean()) if len(size) else 0.0
    score = float(w["single"]) * (1 - single) + float(w["length"]) * length + float(w["concentration"]) * concentration
    return {"score": round(score, 4), "single_event_share": round(single, 4), "median_minutes": round(med, 2),
            "unit_concentration": round(concentration, 4), "episodes": int(len(size))}
