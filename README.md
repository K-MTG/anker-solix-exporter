# Anker SOLIX Exporter

Prometheus exporter for an **Anker SOLIX F2000** used as a UPS, over Bluetooth. No Anker account or cloud. Built on
[SolixBLE](https://github.com/flip-dots/SolixBLE). Tested on one unit, firmware 5.2.3.

It serves `/metrics` on `127.0.0.1:9996` for a local vmagent or Prometheus, and it can keep the AC outlets on.
**It can only ever send two commands: AC on and a status request.** Everything else, including AC off, is refused in
code (see `Device._send_command`).

## Setup

1. Find the unit's MAC. A scan shows it as `Anker SOLIX F2000`. It only advertises when it isn't on Wi-Fi: press its
   pairing button to bring Bluetooth back, which drops Wi-Fi. You lose the Anker app's remote access while it's off Wi-Fi.
2. Enable BlueZ on the host: `systemctl enable --now bluetooth`.
3. Create a client token, any string under 48 bytes, for example a UUID. Anyone in Bluetooth range who has it can talk
   to the unit, so treat it as a secret. Put it in `anker.env` next to the compose file:

   ```
   ANKER_CLIENT_TOKEN=<token>
   ```

4. Set `ANKER_MAC` and `ANKER_UPS_NAME` in `docker-compose.override.yml`, then `docker compose up --build -d`.
5. **Approve it once.** The logs ask for it: hold the pairing button on the unit while its light is solid, within the
   180 s window. Later connections with the same token need no press. A different token needs a new press.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `ANKER_MAC` | required | The unit's Bluetooth address |
| `ANKER_UPS_NAME` | required | The `ups` label on every metric |
| `ANKER_CLIENT_TOKEN` | required | Client token, see above |
| `ANKER_LISTEN_HOST`, `ANKER_LISTEN_PORT` | `127.0.0.1`, `9996` | Where `/metrics` and `/health` listen |
| `ANKER_POLL_SECONDS` | `60` | How often to ask for a full status |
| `ANKER_STALE_SECONDS` | `120` | How long the link can be down before `/metrics` returns 503 |
| `ANKER_REDISCOVER_AFTER_SECONDS` | `60` | How long reconnects can fail before starting over with a fresh scan |
| `ANKER_AC_KEEP_ON` | `false` (`true` in `docker-compose.yml`) | Turn the AC outlets back on if they stay off |
| `ANKER_AC_ON_AFTER_SECONDS` | `60` | How long they must be off first, so a deliberate press has a grace period |
| `ANKER_AC_ON_RETRY_SECONDS` | `300` | Minimum time between AC on commands |

With `ANKER_AC_KEEP_ON=true`, turning the AC off with the unit's button only lasts about a minute. Stop the exporter
first if you need it off for longer.

## Metrics

Names shared with [prometheus-nut-exporter](https://github.com/HON95/prometheus-nut-exporter) mean the same thing here:

| Metric | From | Notes |
|---|---|---|
| `nut_ups_info` | | Model, serial, firmware as labels |
| `nut_status` | `bc` | 1 online, 2 on battery |
| `nut_ups_status{status}` | `bc`, `bf` | `OL`, `OB`, `CHRG`, `DISCHRG` |
| `nut_battery_charge` | `c1` | 0 to 1 |
| `nut_battery_runtime_seconds` | `a4` | Only while discharging |
| `nut_real_power_watts` | `a6` | Only when above 0 |

And for this unit:

| Metric | From | Notes |
|---|---|---|
| `anker_input_watts` | `a5` | Charging power. Reads 0 on mains unless charging, even with a load passing through |
| `anker_ac_output_watts`, `anker_total_output_watts` | `a6`, `b0` | Read 0 at low load (about 27 W read 0) |
| `anker_ac_output_on` | `bb` | 1 on, 0 off |
| `anker_input_state`, `anker_battery_state` | `bc`, `bf` | See the field table |
| `anker_temperature_celsius` | `bd` | |
| `anker_battery_health_ratio` | `c3` | 0 to 1 |
| `anker_time_remaining_seconds` | `a4` | Raw: time to full while charging, noise on mains at 100% |
| `anker_power_saving_enabled` | `db` | From status replies |
| `anker_auto_off_minutes` | `d2` | From status replies. 0 appears to mean never |
| `anker_ble_connected` | | 1 while the session is up |
| `anker_last_telemetry_age_seconds` | | |
| `anker_status_requests_total{result}` | | |
| `anker_ac_on_commands_total` | | AC on commands sent |

`/metrics` returns 503 once the link has been down for `ANKER_STALE_SECONDS`, so `up` goes to 0. `/health` always answers,
and says whether the unit is waiting for a button press.

## Fields, as checked on a real unit

| Field | Meaning |
|---|---|
| `bc` | 2 with AC input (including at 100%), 0 without. Changes within about 25 s |
| `bf` | 0 idle, 1 discharging, 2 charging |
| `bb` | AC outlets on (1) or off (0). Follows the unit's AC button |
| `a5`, `af` | Input watts; matched the display at 224 W and 1346 W. Same value with no solar connected |
| `a6`, `b0` | Output watts; matched the display at 27 W. About 400 W for a hair dryer on low |
| `a4` | Tenths of an hour. Sensible on battery: about 50 h at low load, 4.5 h at 400 W |

## Tests

```sh
pip install SolixBLE==4.0.0b3 pytest
python -m pytest
```

## Not affiliated

Not affiliated with or endorsed by Anker.
