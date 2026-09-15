"""
Bioreactor program (schedule) parser + dry-run expander.

A *program* is a JSON document of one or more parallel per-device **tracks**, each a
timeline of ``{command, "for": duration}`` steps. Tracks run concurrently for the
program's total ``duration``; a track may ``repeat`` its steps. Example:

    {
      "name": "light cycle + temp",
      "duration": "10d",
      "tracks": [
        {"name": "lights", "repeat": true,
         "steps": [{"ring": [100,100,100], "for": "12h"}, {"ring": [0,0,0], "for": "12h"}]},
        {"name": "temperature",
         "steps": [{"temp": 30, "for": "1h"}, {"temp": 25}]}
      ]
    }

Commands (one per step; the key names the device):
    ring: [r,g,b]     -> ring light          (device 'ring')
    temp: <°C>        -> PID setpoint         (device 'peltier', closed loop)
    heater: <±%>      -> open-loop duty       (device 'peltier', +heat / -cool)
    stirrer: <%>      -> stirrer duty         (device 'stirrer')
    pump: {duty:<%>, interval:<dur>} -> timed media-exchange dosing (device 'pump')
    relay: {name:<relay>, state:open|closed} -> relay state (device 'relay:<name>')
    od: {power:<%>, enabled?:bool} -> OD IR-LED sampling power/enable (device 'od')
    co2: <ppm> | false -> CO2 MPC target / stop (device 'relay:CO2')

Durations: bare number = seconds; suffix s/m/h/d ("90", "10m", "12h", "10d").
Omitting "for" (or <=0) on the LAST step of a non-repeating track = hold until the
program ends.

This module only PARSES/VALIDATES and can EXPAND a program to a flat event timeline
for inspection. Execution lives in control.py.
"""
from __future__ import annotations

import re
import json
import math
from typing import Any, Dict, List, Optional


class ProgramError(ValueError):
    """Raised on a malformed / invalid program."""


# which device each command controls (for one-track-per-device + override tracking)
_COMMAND_DEVICE = {
    'ring': 'ring',
    'temp': 'peltier',
    'heater': 'peltier',
    'stirrer': 'stirrer',
    'pump': 'pump',
    'relay': 'relay',   # per-relay device is 'relay:<name>' (resolved in _parse_command)
    'od': 'od',
    'co2': 'relay:CO2',  # excludes a competing manual CO2 relay track
}
_COMMANDS = tuple(_COMMAND_DEVICE)

_UNIT_SECONDS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}

DEFAULT_LIMITS = {'max_heat': 70.0, 'max_cool': 100.0, 'temp_min': 2.0, 'temp_max': 60.0}


def parse_duration(v) -> Optional[float]:
    """Parse a duration to seconds. Bare number = seconds; suffix s/m/h/d.
    None / '' / <= 0 -> None (meaning 'hold until the program ends')."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise ProgramError(f"bad duration {v!r}")
    if isinstance(v, (int, float)):
        secs = float(v)
    else:
        s = str(v).strip().lower()
        if not s:
            return None
        m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([smhd]?)', s)
        if not m:
            raise ProgramError(f"bad duration {v!r} (use e.g. 90, 10m, 12h, 10d)")
        secs = float(m.group(1)) * _UNIT_SECONDS[m.group(2) or 's']
    return secs if secs > 0 else None


def _num(v, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ProgramError(f"{what} must be a number, got {v!r}")
    return float(v)


class Step:
    __slots__ = ('device', 'command', 'value', 'duration_s')

    def __init__(self, device: str, command: str, value: Any, duration_s: Optional[float]):
        self.device = device            # 'ring' | 'peltier' | 'stirrer'
        self.command = command          # 'ring' | 'temp' | 'heater' | 'stirrer'
        self.value = value              # normalized: (r,g,b) tuple | float
        self.duration_s = duration_s    # float | None (None = hold to program end)

    def __repr__(self):
        d = "hold" if self.duration_s is None else f"{self.duration_s:g}s"
        return f"Step({self.command}={self.value}, {d})"


class Track:
    __slots__ = ('name', 'device', 'repeat', 'steps')

    def __init__(self, name: str, device: str, repeat: bool, steps: List[Step]):
        self.name = name
        self.device = device
        self.repeat = repeat
        self.steps = steps


class Program:
    __slots__ = ('name', 'duration_s', 'tracks', 'gains')

    def __init__(self, name, duration_s, tracks, gains=None):
        self.name = name
        self.duration_s = duration_s     # float | None
        self.tracks = tracks
        self.gains = gains               # optional {'kp','ki','kd'} for temp/PID steps


# ---------------------------------------------------------------------------
def _parse_command(obj: dict, limits: dict):
    """Return (device, command, normalized_value) for a single step's command."""
    cmds = [k for k in obj if k in _COMMAND_DEVICE]
    if len(cmds) != 1:
        raise ProgramError(
            f"each step needs exactly one command ({', '.join(_COMMANDS)}), got {sorted(obj)}")
    cmd = cmds[0]
    device = _COMMAND_DEVICE[cmd]
    raw = obj[cmd]

    if cmd == 'co2':
        if raw is False:
            return device, cmd, False
        val = _num(raw, 'co2 target (ppm)')
        if not math.isfinite(val) or not 0 < val < 95000:
            raise ProgramError('co2 target must be finite, positive and below 95000 ppm')
        return device, cmd, val

    if cmd == 'ring':
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            raise ProgramError(f"ring needs [r, g, b], got {raw!r}")
        try:
            rgb = tuple(int(x) for x in raw)
        except (TypeError, ValueError):
            raise ProgramError(f"ring values must be integers, got {raw!r}")
        if any(not (0 <= x <= 255) for x in rgb):
            raise ProgramError(f"ring values must be 0-255, got {raw!r}")
        return device, cmd, rgb

    if cmd == 'temp':
        val = _num(raw, 'temp')
        lo, hi = limits['temp_min'], limits['temp_max']
        if not (lo <= val <= hi):
            raise ProgramError(f"temp {val} out of range [{lo:g}, {hi:g}] °C")
        return device, cmd, val

    if cmd == 'heater':
        val = _num(raw, 'heater')
        lim = limits['max_heat'] if val >= 0 else limits['max_cool']
        if abs(val) > lim:
            raise ProgramError(
                f"heater {val:g}% exceeds {lim:g}% limit ({'heat' if val >= 0 else 'cool'})")
        return device, cmd, val

    if cmd == 'stirrer':
        val = _num(raw, 'stirrer')
        if not (0.0 <= val <= 100.0):
            raise ProgramError(f"stirrer {val:g} out of range [0, 100]%")
        return device, cmd, val

    if cmd == 'pump':
        # {"duty": 0-100, "interval": <duration>, "rate"?: <ml/s>} (or [duty, interval]
        # / [duty, interval, rate]). Cycles every `interval`: outflow on for
        # interval*duty, inflow for 0.95*interval*duty. `rate` is optional.
        rate_raw = None
        if isinstance(raw, dict):
            duty_raw, interval_raw, rate_raw = raw.get('duty'), raw.get('interval'), raw.get('rate')
        elif isinstance(raw, (list, tuple)) and len(raw) in (2, 3):
            duty_raw, interval_raw = raw[0], raw[1]
            rate_raw = raw[2] if len(raw) == 3 else None
        else:
            raise ProgramError('pump needs {"duty": 0-100, "interval": <seconds>, "rate"?: <ml/s>}'
                               ' or [duty, interval] / [duty, interval, rate]')
        duty = _num(duty_raw, 'pump duty')
        if not (0.0 <= duty <= 100.0):
            raise ProgramError(f"pump duty {duty:g} out of range [0, 100]%")
        interval = parse_duration(interval_raw)
        if interval is None:
            raise ProgramError(f"pump interval must be a positive duration, got {interval_raw!r}")
        value = {'duty': duty, 'interval': interval}
        if rate_raw is not None:
            rate = _num(rate_raw, 'pump rate')
            if rate < 0:
                raise ProgramError(f"pump rate {rate:g} must be >= 0 ml/s")
            value['rate'] = rate
        return device, cmd, value

    if cmd == 'relay':
        # {"name": <relay>, "state": "open"|"closed"} (or [name, state]). One relay per
        # track; device is 'relay:<name>' so manual overrides track per relay.
        rr = obj['relay']
        if isinstance(rr, dict):
            rname, rstate = rr.get('name'), rr.get('state')
        elif isinstance(rr, (list, tuple)) and len(rr) == 2:
            rname, rstate = rr
        else:
            raise ProgramError('relay needs {"name": <relay>, "state": "open"|"closed"} or [name, state]')
        if not isinstance(rname, str) or not rname:
            raise ProgramError(f"relay name must be a non-empty string, got {rname!r}")
        rstate = str(rstate).lower()
        if rstate not in ('open', 'closed'):
            raise ProgramError(f"relay state must be 'open' or 'closed', got {rstate!r}")
        return f'relay:{rname}', cmd, {'name': rname, 'state': rstate}

    # od: {"power": 0-100, "enabled"?: bool}  (or a bare number = power). Sets the OD
    # IR-LED sampling power / enabled; device 'od'. Re-assert it on a schedule to reclaim
    # manual OD-tab changes at each step boundary.
    raw_od = obj['od']
    if isinstance(raw_od, dict):
        power_raw, enabled_raw = raw_od.get('power'), raw_od.get('enabled')
    else:
        power_raw, enabled_raw = raw_od, None
    power = _num(power_raw, 'od power')
    if not (0.0 <= power <= 100.0):
        raise ProgramError(f"od power {power:g} out of range [0, 100]%")
    val = {'power': power}
    if enabled_raw is not None:
        val['enabled'] = bool(enabled_raw)
    return device, cmd, val


def _parse_track(obj: dict, limits: dict, index: int) -> Track:
    if not isinstance(obj, dict):
        raise ProgramError(f"track {index} must be an object")
    name = str(obj.get('name') or f"track{index + 1}")
    repeat = bool(obj.get('repeat', False))
    raw_steps = obj.get('steps')
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ProgramError(f"track '{name}' needs a non-empty 'steps' list")

    steps: List[Step] = []
    for i, s in enumerate(raw_steps):
        if not isinstance(s, dict):
            raise ProgramError(f"track '{name}' step {i + 1} must be an object")
        device, cmd, value = _parse_command(s, limits)
        dur = parse_duration(s.get('for'))
        steps.append(Step(device, cmd, value, dur))

    devices = {s.device for s in steps}
    if len(devices) > 1:
        raise ProgramError(f"track '{name}' mixes devices {sorted(devices)}; "
                           f"one track controls one device")
    device = devices.pop()

    # open-ended (hold-to-end) steps: only the last step of a non-repeating track
    for i, s in enumerate(steps):
        if s.duration_s is None:
            if repeat:
                raise ProgramError(f"track '{name}': a repeating track can't have an "
                                   f"open-ended step (every step needs a 'for')")
            if i != len(steps) - 1:
                raise ProgramError(f"track '{name}': only the last step may omit 'for' "
                                   f"(hold to end)")
    return Track(name, device, repeat, steps)


def parse_program(data, limits: Optional[dict] = None) -> Program:
    """Parse + validate a program from a dict or a JSON string. Raises ProgramError."""
    lim = {**DEFAULT_LIMITS, **(limits or {})}
    if isinstance(data, (str, bytes, bytearray)):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            raise ProgramError(f"invalid JSON: {e}")
    if not isinstance(data, dict):
        raise ProgramError("program must be a JSON object")

    raw_tracks = data.get('tracks')
    if not isinstance(raw_tracks, list) or not raw_tracks:
        raise ProgramError("program needs a non-empty 'tracks' list")

    tracks = [_parse_track(t, lim, i) for i, t in enumerate(raw_tracks)]

    seen: Dict[str, str] = {}
    for t in tracks:
        if t.device in seen:
            raise ProgramError(f"two tracks control the same device '{t.device}' "
                               f"('{seen[t.device]}' and '{t.name}')")
        seen[t.device] = t.name

    duration_s = parse_duration(data.get('duration'))
    if any(t.repeat for t in tracks) and duration_s is None:
        raise ProgramError("'duration' is required when any track repeats "
                           "(otherwise it would run forever)")

    gains = None
    pid = data.get('pid')
    if pid is not None:
        if not isinstance(pid, dict):
            raise ProgramError("'pid' must be an object of {kp, ki, kd}")
        gains = {}
        for k in ('kp', 'ki', 'kd'):
            if k in pid:
                gains[k] = _num(pid[k], f"pid.{k}")

    name = data.get('name')
    return Program(str(name) if name is not None else None, duration_s, tracks, gains)


# ---------------------------------------------------------------------------
def expand(program: Program, max_events: int = 500) -> List[Dict[str, Any]]:
    """Flatten to a time-sorted list of events for dry-run inspection:
    [{t, track, device, command, value}, ...] up to the program duration
    (capped at max_events per track to bound repeats)."""
    end = program.duration_s
    events: List[Dict[str, Any]] = []
    for track in program.tracks:
        t = 0.0
        idx = 0
        count = 0
        while count < max_events:
            if end is not None and t >= end:
                break
            step = track.steps[idx]
            events.append({'t': round(t, 3), 'track': track.name, 'device': step.device,
                           'command': step.command, 'value': step.value})
            count += 1
            if step.duration_s is None:
                break                       # holds to end
            t += step.duration_s
            idx += 1
            if idx >= len(track.steps):
                if track.repeat:
                    idx = 0
                else:
                    break
    events.sort(key=lambda e: e['t'])
    return events


def preview_extent(program) -> float:
    """The time span to draw a preview over: the program duration, or (if none) the
    longest track's one-pass finite length."""
    if program.duration_s is not None:
        return program.duration_s
    spans = [sum(s.duration_s for s in t.steps if s.duration_s is not None)
             for t in program.tracks]
    return max(spans, default=0.0) or 3600.0


def expand_tracks(program, max_segments: int = 400):
    """Per-track segment timeline for a preview:
      [{name, device, repeat, segments: [{start, end, command, value}]}]
    Times in seconds, clipped to preview_extent(); repeat tracks fill it and
    open-ended (hold) steps extend to it."""
    extent = preview_extent(program)
    out = []
    for track in program.tracks:
        segs = []
        t = 0.0
        idx = 0
        count = 0
        while count < max_segments and t < extent:
            step = track.steps[idx]
            if step.duration_s is None:                 # hold to end
                segs.append({'start': round(t, 3), 'end': round(extent, 3),
                             'command': step.command, 'value': step.value})
                break
            seg_end = min(t + step.duration_s, extent)
            segs.append({'start': round(t, 3), 'end': round(seg_end, 3),
                         'command': step.command, 'value': step.value})
            count += 1
            t += step.duration_s
            idx += 1
            if idx >= len(track.steps):
                if track.repeat:
                    idx = 0
                else:
                    break
        out.append({'name': track.name, 'device': track.device,
                    'repeat': track.repeat, 'segments': segs})
    return {'extent_s': round(extent, 3), 'tracks': out}
