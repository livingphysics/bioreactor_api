"""Validation shared by live optical/gas readings and archived plot points."""
import math

CO2_MAX_READING_PPM = 200_000
CO2_HISTORY_KEYS = ('co2', 'master_co2', 'co2_reference')


def co2_or_none(value):
    """Use JSON null for missing/invalid CO2; exactly 200,000 ppm remains valid."""
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or value > CO2_MAX_READING_PPM
            or (isinstance(value, float) and not math.isfinite(value))):
        return None
    return value


def clean_co2_point(point):
    """Keep timestamps and other channels when an archived CO2 value is invalid."""
    return {key: co2_or_none(value) if key in CO2_HISTORY_KEYS else value
            for key, value in point.items()}


def od_or_none(value):
    """Negative optical readings are missing; retain zero and positive values."""
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or value < 0 or (isinstance(value, float) and not math.isfinite(value))):
        return None
    return value


def clean_sensor_point(point):
    """Clean gas and optical channels without changing timestamps or actuators."""
    cleaned = clean_co2_point(point)
    for key in ('od', 'volt'):
        if isinstance(cleaned.get(key), dict):
            cleaned[key] = {name: od_or_none(value) for name, value in cleaned[key].items()}
    return cleaned
