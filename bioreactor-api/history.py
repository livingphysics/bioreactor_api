"""
Rolling sensor-history buffer + long-term daily archive for the bioreactor API.

A background thread samples the monitor signals (bath temp, ambient temp, signed
peltier current, gas, OD channels) plus actuator/control state (signed peltier duty,
ring RGB, stirrer duty, IR-LED power, active setpoint) every `interval_s`.

Two layers:
  * In-memory ring buffer — the last `window_s` seconds (default 24h). This is what
    GET /api/history serves; the frontend is unchanged.
  * On-disk daily archive — every sample is APPENDED (never rewritten) as one
    truncated JSON line to `history/YYYY-MM-DD.jsonl`. A new file starts each local
    day; files older than `retention_days` (default 365) are pruned. Appending only
    the new bytes keeps SD-card writes tiny (~MB/day) vs rewriting a whole file.

On startup the ring buffer is reloaded from the most recent daily files (falling
back once to the legacy single-file `sensor_history.json` if present), so the live
view survives a restart. Independent of runs — it logs continuously.
"""
import os
import glob
import json
import time
import logging
import threading
from collections import deque
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _num(v, ndigits=None):
    """Keep finite numbers (optionally rounded), drop None/NaN/non-numeric -> None."""
    if not (isinstance(v, (int, float)) and v == v):
        return None
    return round(float(v), ndigits) if ndigits is not None else v


class HistoryBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._buf = deque()
        self._thread = None
        self._stop = threading.Event()
        self._sample_fn = None
        self._dir = None                 # daily-archive directory
        self._legacy_path = None         # old single-file buffer (one-time migration)
        self._interval = 10.0
        self._window_s = 24 * 3600       # in-memory window served to the frontend
        self._retention_days = 365       # daily archive files kept on disk
        # open append handle for the current day's file
        self._cur_date = None
        self._cur_file = None
        self._last_prune = 0.0
        self._archive_fail = 0        # consecutive archive-append failures
        self._last_fail_log = 0.0     # throttle the failure warning
        self._earliest_cache = None   # oldest archived timestamp (ms), cached
        self._earliest_at = 0.0       # when _earliest_cache was computed

    @property
    def interval_s(self) -> float:
        return self._interval

    def configure(self, *, sample_fn, archive_dir, interval_s=10.0,
                  window_s=24 * 3600, retention_days=365, legacy_path=None):
        self._sample_fn = sample_fn
        self._dir = archive_dir
        self._legacy_path = legacy_path
        self._interval = max(1.0, float(interval_s))
        self._window_s = max(60, int(window_s))
        self._retention_days = max(1, int(retention_days))
        self._buf = deque(maxlen=int(self._window_s / self._interval) + 120)
        if self._dir:
            try:
                os.makedirs(self._dir, exist_ok=True)
            except Exception as e:
                logger.error("history: cannot create archive dir %s: %s", self._dir, e)
                self._dir = None
        self._load_recent()

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self._sample_fn is None:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="history")
        self._thread.start()
        logger.info("History sampler started (every %.0fs, %dh live window, %dd archive)",
                    self._interval, self._window_s // 3600, self._retention_days)

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and threading.current_thread() is not t:
            t.join(timeout=3.0)
        self._close_file()

    def _run(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._sample_once()
            except Exception as e:
                logger.error("history sample failed: %s", e)
            if time.time() - self._last_prune >= 3600:   # prune old daily files hourly
                try:
                    self._prune()
                except Exception as e:
                    logger.warning("history prune failed: %s", e)
            self._stop.wait(max(0.0, self._interval - (time.time() - t0)))

    # -------------------------------------------------------------------- sampling
    def _sample_once(self):
        data = self._sample_fn()
        if not data:
            return
        now_ms = int(time.time() * 1000)
        pt = {
            "t": now_ms,
            "temp": _num(data.get("temperature"), 3),
            "ambient": _num(data.get("ambient_temp"), 3),
            "current": _num(data.get("peltier_current"), 4),
            "co2": _num(data.get("co2")),         # ppm (int)
            "o2": _num(data.get("o2"), 2),        # % (2 dp)
        }
        # Actuator + control state. Added only when present, so points stay compact and
        # pre-existing archive lines (which lack these keys) stay valid — consumers treat
        # a missing key as null. pduty is signed: + cooling, - heating (like current).
        for key, src, nd in (("pduty", "peltier_duty", 1), ("stir", "stirrer", 1),
                             ("ir", "ir_power", 1), ("setpoint", "setpoint", 2),
                             ("pump", "pump_duty", 1)):
            v = _num(data.get(src), nd)
            if v is not None:
                pt[key] = v
        ring = data.get("ring")
        if isinstance(ring, (list, tuple)) and len(ring) == 3:
            pt["ring"] = [int(ring[0]), int(ring[1]), int(ring[2])]
        relays = data.get("relays")
        if isinstance(relays, dict) and relays:
            pt["relays"] = {k: int(v) for k, v in relays.items()}
        # Cumulative dose counters — capture pump/relay events shorter than the sample
        # interval (diff successive points for the per-interval amount).
        relay_closed = data.get("relay_closed_s")
        if isinstance(relay_closed, dict) and relay_closed:
            pt["relay_closed_s"] = {k: _num(v, 2) for k, v in relay_closed.items()}
        pump_time = data.get("pump_time_s")
        if isinstance(pump_time, dict) and pump_time:
            pt["pump_time_s"] = {k: _num(v, 2) for k, v in pump_time.items()}
        od = data.get("od")
        if isinstance(od, dict):
            pt["od"] = {k: _num(v, 5) for k, v in od.items()}   # OD measurements (OD_x -> V)
        volt = data.get("volt")
        if isinstance(volt, dict) and volt:
            pt["volt"] = {k: _num(v, 5) for k, v in volt.items()}   # unmapped voltage sources
        with self._lock:
            self._buf.append(pt)
            self._evict()
        self._append_archive(pt, now_ms)

    def _append_archive(self, pt, now_ms):
        """Append one JSON line to today's daily file (opening/rotating as needed)."""
        if not self._dir:
            return
        date = datetime.fromtimestamp(now_ms / 1000).strftime("%Y-%m-%d")
        try:
            if date != self._cur_date or self._cur_file is None:
                self._close_file()
                os.makedirs(self._dir, exist_ok=True)   # self-heal a transiently-missing dir
                self._cur_file = open(os.path.join(self._dir, f"{date}.jsonl"), "a")
                self._cur_date = date
            self._cur_file.write(json.dumps(pt, separators=(",", ":")) + "\n")
            self._cur_file.flush()   # durable to the OS every sample; OS coalesces disk writes
            if self._archive_fail:
                logger.info("history archive recovered after %d failed sample(s)", self._archive_fail)
                self._archive_fail = 0
        except Exception as e:
            # Throttle: warn on the first failure, then at most every 5 min, so a full
            # disk / missing dir doesn't spam a warning every sample. Reopen next tick
            # (with the makedirs above) so a transient failure self-heals.
            self._archive_fail += 1
            now = time.time()
            if self._archive_fail == 1 or now - self._last_fail_log >= 300:
                logger.warning("history archive append failing (%d sample(s)): %s",
                               self._archive_fail, e)
                self._last_fail_log = now
            self._close_file()       # force a clean reopen next tick

    def _close_file(self):
        if self._cur_file is not None:
            try:
                self._cur_file.close()
            except Exception:
                pass
        self._cur_file = None
        self._cur_date = None

    def _evict(self):
        cutoff = int(time.time() * 1000) - self._window_s * 1000
        while self._buf and self._buf[0]["t"] < cutoff:
            self._buf.popleft()

    def get(self, since_ms=0):
        with self._lock:
            self._evict()
            if since_ms and since_ms > 0:
                return [p for p in self._buf if p["t"] > since_ms]
            return list(self._buf)

    def read_range(self, start_ms, end_ms=None, max_points=6000):
        """Read archived points with start_ms <= t <= end_ms straight from the on-disk
        daily files. The in-memory buffer only spans window_s (24h), so this backs the
        'since program start' view, which can reach back further. Points are sorted and,
        for very long ranges, downsampled by a fixed stride to max_points so the payload
        and the client redraw stay cheap."""
        end_ms = int(end_ms) if end_ms else int(time.time() * 1000)
        start_ms = int(start_ms)
        if start_ms <= 0 or start_ms >= end_ms or not self._dir:
            return []
        out = []
        day = datetime.fromtimestamp(start_ms / 1000).date()
        last = datetime.fromtimestamp(end_ms / 1000).date()
        while day <= last:
            path = os.path.join(self._dir, day.strftime("%Y-%m-%d") + ".jsonl")
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                p = json.loads(line)
                            except Exception:
                                continue
                            if isinstance(p, dict) and start_ms <= p.get("t", 0) <= end_ms:
                                out.append(p)
                except Exception as e:
                    logger.warning("history read_range %s failed: %s", os.path.basename(path), e)
            day += timedelta(days=1)
        out.sort(key=lambda p: p.get("t", 0))
        if max_points and len(out) > max_points:
            stride = (len(out) + max_points - 1) // max_points
            out = out[::stride]
        return out

    def earliest_ms(self):
        """Timestamp (ms) of the oldest point in the on-disk archive, or None if empty.
        Backs the 'max' view when no program is running (full recorded history). Cached
        for an hour since it only moves as old daily files are pruned."""
        now = time.time()
        if self._earliest_cache is not None and (now - self._earliest_at) < 3600:
            return self._earliest_cache
        earliest = None
        if self._dir:
            for path in sorted(glob.glob(os.path.join(self._dir, "*.jsonl"))):
                try:
                    with open(path) as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                p = json.loads(line)
                            except Exception:
                                continue
                            if isinstance(p, dict) and "t" in p:
                                earliest = int(p["t"])
                                break
                except Exception:
                    continue
                if earliest is not None:
                    break
        self._earliest_cache = earliest
        self._earliest_at = now
        return earliest

    # ------------------------------------------------------------------ archive I/O
    def _load_recent(self):
        """Repopulate the in-memory window from the newest daily files (or the legacy
        single-file buffer, once) so the live view survives a restart."""
        cutoff = int(time.time() * 1000) - self._window_s * 1000
        recent = []
        if self._dir:
            days_needed = int(self._window_s / 86400) + 2   # enough files to cover the window
            for path in sorted(glob.glob(os.path.join(self._dir, "*.jsonl")))[-days_needed:]:
                try:
                    with open(path) as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                p = json.loads(line)
                            except Exception:
                                continue
                            if isinstance(p, dict) and p.get("t", 0) >= cutoff:
                                recent.append(p)
                except Exception as e:
                    logger.warning("history load %s failed: %s", os.path.basename(path), e)
        # Bridge in legacy single-file-buffer points that predate the archive, so the
        # live window stays full during the first 24h after upgrading (before the daily
        # archive has accumulated a full window). Only points older than the archive's
        # earliest are added, so there's no overlap; they age out of the window
        # naturally, and once the archive covers the whole window this contributes
        # nothing.
        if self._legacy_path and os.path.exists(self._legacy_path):
            try:
                with open(self._legacy_path) as f:
                    obj = json.load(f)
                earliest = min((p["t"] for p in recent), default=float("inf"))
                legacy = [p for p in obj.get("points", [])
                          if isinstance(p, dict) and cutoff <= p.get("t", 0) < earliest]
                if legacy:
                    recent = legacy + recent
                    logger.info("History: bridged %d legacy points into the live window", len(legacy))
            except Exception as e:
                logger.warning("legacy history load failed: %s", e)
        recent.sort(key=lambda p: p.get("t", 0))
        with self._lock:
            self._buf.extend(recent)   # deque maxlen bounds it
        logger.info("History: loaded %d recent points", len(recent))

    def _prune(self):
        """Delete daily archive files older than retention_days."""
        self._last_prune = time.time()
        if not self._dir:
            return
        cutoff = datetime.now().date() - timedelta(days=self._retention_days)
        for path in glob.glob(os.path.join(self._dir, "*.jsonl")):
            name = os.path.basename(path)[:-6]   # strip ".jsonl"
            try:
                day = datetime.strptime(name, "%Y-%m-%d").date()
            except ValueError:
                continue   # not a dated archive file
            if day < cutoff:
                try:
                    os.remove(path)
                    logger.info("History: pruned archive %s", name)
                except Exception as e:
                    logger.warning("prune %s failed: %s", name, e)


# Module-level singleton used by main.py
history = HistoryBuffer()
