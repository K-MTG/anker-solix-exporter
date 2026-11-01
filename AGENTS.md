# Agent notes

Load-bearing details. See `README.md` for everything else.

- **Never add a way to send anything other than AC on and the status request.** The unit is a UPS: a stray "off" cuts
  power to what it protects, possibly including this host's network. `Device._send_command` is the gate, and tests in
  `tests/test_exporter.py` hold it in place. Don't import `PARAMETERS_OFF` here.
- **SolixBLE is pinned to 4.0.0b3 on purpose.** 3.9.0 ignores the advertised capability byte, and the unit drops the
  link on its first message. Pass the advertisement to the constructor, or the first attempt can use the wrong handshake.
- **Don't restart the handshake during the button window.** It voids a press in progress (`_negotiation_should_restart`).
- **Leave ~40 s between attempts after a failed handshake.** A quick retry got replies that wouldn't decrypt.
- **A watt reading of 0 means "below what the unit reports", not "no load".** About 27 W read 0 outside charging.
  That's why `anker_real_power_watts` is left out at 0.
- **Metrics use the `anker_` prefix, never `nut_`.** The ones mirroring the NUT exporter keep its names after the prefix.
- **Don't invent NUT flags.** The unit reports no replace-battery flag, so `RB` isn't exported.
- **SolixBLE's auto-reconnect never rescans.** A `bluetoothd` restart (e.g. an unattended bluez upgrade) makes BlueZ
  forget every device, and its saved device object then fails until something scans. `Exporter.supervise` handles this:
  don't remove it. It must not rescan while the unit waits for its button, which would void a press.
- **Back off AC on.** The outlets may have tripped on a real fault, so don't retry quickly or forever at a fast rate.
- **Exit if a loop dies.** `/metrics` staying up with a dead reconnect loop would look healthy. Docker restarts us.
- **Disconnect on shutdown.** A stale link keeps the unit from advertising, so the next scan can't find it.
