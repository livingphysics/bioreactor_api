# Bioreactor API Reference

Base URL: `https://issued-fantasy-fighter-specials.trycloudflare.com`

All endpoints require authentication:
```
Authorization: Bearer <API_KEY>
```

---

## System

### Health Check
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/health
```

### List Capabilities
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/capabilities
```

### Aggregate State (for the live monitor)
One call returns bath temp, ambient temp, signed peltier current, peltier
duty/direction, and heater-run status — poll this at ~1 Hz instead of hitting
each sensor endpoint separately.
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/state
```
Response: `{"status": "success", "temperature": 24.2, "ambient_temp": 24.4, "peltier_current": 0.02, "peltier": {"duty_cycle": 0, "direction": "cool", "active": false}, "heater": {...}}`

---

## Actuators

### LED

**Get state:**
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/led/state
```

**Set power (0-100%):**
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"power": 50}' https://<host>/api/led/control
```

---

### Peltier (Temperature Control)

**Get state:**
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/peltier_driver/state
```

**Set duty cycle and direction:**
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"duty_cycle": 50, "direction": "heat"}' https://<host>/api/peltier_driver/control
```

Direction options: `heat`, `cool`, `forward`, `reverse`

---

### Stirrer

**Get state:**
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/stirrer/state
```

**Set duty cycle (0-100%):**
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"duty_cycle": 50}' https://<host>/api/stirrer/control
```

---

### Ring Light

**Get state:**
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/ring_light/state
```

**Set color (RGB 0-255):**
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"red": 30, "green": 30, "blue": 30}' https://<host>/api/ring_light/control
```

**Set specific pixel:**
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"red": 255, "green": 0, "blue": 0, "pixel_index": 0}' https://<host>/api/ring_light/control
```

---

### Pumps

**Get state:**
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/pumps/state
```

**Control pump (velocity in mL/s, negative for reverse):**
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"pump_name": "inflow", "velocity": 1.5}' https://<host>/api/pumps/control
```

---

### Relays

**Get state:**
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/relays/state
```

**Set relay state:**
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"relay_name": "heater", "state": true}' https://<host>/api/relays/control
```

---

## Sensors

### Temperature
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/temp_sensor/state
```
Response: `{"status": "success", "temperature": 23.5, "unit": "celsius"}`

---

### Ambient Temperature
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/ambient_temp/state
```
Response: `{"status": "success", "temperature": 22.4, "unit": "celsius"}`

---

### Peltier Current
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/peltier_current/state
```
Response: `{"status": "success", "current": 1.73, "unit": "amps"}`

---

### CO2
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/co2_sensor/state
```
Response: `{"status": "success", "co2_ppm": 415.2, "unit": "ppm"}`

---

### O2
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/o2_sensor/state
```
Response: `{"status": "success", "o2_percent": 20.9, "unit": "percent"}`

---

### Optical density (OD measurements)

The four canonical OD measurements `OD_45` / `OD_ref` / `OD_90` / `OD_135` are configured
in `config.py` (`OD_MEASUREMENTS`), each fed by a named voltage source (`VOLTAGE_SOURCES`,
an ADS1115 channel or an eyespy board). Values come from the IR-gated background sampler;
`null` = not measured yet / sampling off.
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/od/state
```
Response: `{"status": "success", "od": {"OD_ref": 0.81, "OD_90": 1.08, "OD_135": 0.50}, "measurements": {"OD_ref": "pd_ref", "OD_90": "pd_90", "OD_135": "pd_135"}, "available": true, "sampling": {"enabled": true, "led_power": 10, "kinds": ["adc", "eyespy"], ...}, "unit": "volts"}`

The same `od`, `od_measurements`, `od_available` and `od_sampling` fields are in `GET /api/state`.

**IR-gated sampling control** (on/off + LED power for each gated reading):
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"enabled": true, "led_power": 10}' https://<host>/api/od/sampling
```

---

### Voltage sources

Every configured source by name, with its kind, the OD measurements it feeds, the last
IR-gated reading (`gated`) and an instantaneous un-gated reading taken now (`live`).
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/voltages
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/voltage/pd_135
```
Response (single): `{"status": "success", "name": "pd_135", "kind": "adc", "component": "optical_density", "od": ["OD_135"], "gated": 0.504, "live": 0.51, "unit": "volts"}`
(`404` for an unknown name, `503` if the source's hardware component is down.)

Deprecated aliases, kept for old scripts: `GET /api/optical_density/state` and
`GET /api/eyespy_adc/state` return the un-gated voltages of the ADS1115 / eyespy sources as a
positional list plus `names`.

---

## Heater Control (schedule / PID)

The control loop runs on the Pi with safety cutoffs (peltier off if the bath
temperature reads NaN for 15 samples or leaves the 2–60 °C window). While a run
is active, manual `POST /api/peltier_driver/control` returns `409`.

**Upload + run a schedule** (CSV body, `duty,direction,hold_s`, same format as heater_gui):
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: text/csv" \
  --data-binary $'duty,direction,hold_s\n50,cool,120\n0,heat,30\n70,heat,60\n' \
  https://<host>/api/heater/schedule
```

**Run a PID setpoint:**
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"setpoint": 37.0, "kp": 12.0, "ki": 0.015, "kd": 0.0}' \
  https://<host>/api/heater/pid
```

**Status of the current run:**
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/heater/status
```
Response: `{"active": true, "mode": "schedule", "step": 2, "total_steps": 3, "last": {"temperature": 24.2, "ambient_temp": 24.4, "peltier_current": -0.36, ...}, ...}`

**Stop any active run (peltier off):**
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" https://<host>/api/heater/stop
```

---

## Data Files

**Download the most recent run CSV:**
```bash
curl -H "Authorization: Bearer $API_KEY" -OJ https://<host>/api/data/latest
```

**List available data files (newest first):**
```bash
curl -H "Authorization: Bearer $API_KEY" https://<host>/api/data/list
```

---

## Error Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 400 | Malformed schedule / body |
| 401 | Missing authorization header |
| 403 | Invalid API key |
| 404 | No data file found |
| 409 | Manual control blocked (a heater run is active) / run already active |
| 429 | Rate limit exceeded (100 req/min) |
| 503 | Component not available |
