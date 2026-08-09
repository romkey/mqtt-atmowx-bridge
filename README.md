# mqtt-atmowx-bridge

[![CI](https://github.com/romkey/mqtt-atmowx-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/romkey/mqtt-atmowx-bridge/actions/workflows/ci.yml)
[![Lint](https://github.com/romkey/mqtt-atmowx-bridge/actions/workflows/lint.yml/badge.svg)](https://github.com/romkey/mqtt-atmowx-bridge/actions/workflows/lint.yml)
[![Docker](https://github.com/romkey/mqtt-atmowx-bridge/actions/workflows/docker.yml/badge.svg)](https://github.com/romkey/mqtt-atmowx-bridge/actions/workflows/docker.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Publishes weather observations from an MQTT broker to the AT Protocol as
[`net.atmowx.observation`](https://tangled.org/atmowx.net) records, in the spirit
of [awn-bridge](https://tangled.org/atmowx.net/awn-bridge) but reading from MQTT
instead of the Ambient Weather API.

Point it at whatever your station already publishes — an Ambient Weather or
Ecowitt console bridged to MQTT, weewx's MQTT uploader, Home Assistant state
topics, or a handful of ESP32s posting bare numbers — describe the topics in
YAML, and it handles unit conversion, record assembly and token management.

## What it does

**Publishes only what you actually measured.** A field absent from MQTT is
absent from the record. No zeros standing in for sensors you do not own, and a
sensor that stops reporting drops out of subsequent records rather than
flatlining at its last value.

**Converts to the SI units the lexicon requires.** °F to °C, inHg to hPa, mph to
m/s, in/hr to mm/h, lux to W/m². Units are checked against the field's dimension
when the config loads, so feeding inches of mercury into a wind speed is a
startup error, not a strange record.

**Encodes decimals exactly.** The lexicon has no floating point type, so values
are scaled integers (`{"value": 281, "scale": -1}` is 28.1 °C), rounded to the
precision the sensor actually resolves.

**Manages JWTs properly** — see [Token management](#token-management).

**Uses deterministic record keys.** The key is a TID derived from `observedAt`,
so republishing a reading overwrites it instead of creating a duplicate. That is
what makes the retry queue safe when your PDS is briefly unreachable.

**Fills in derived values on request.** Dew point from temperature and humidity;
sea-level pressure from station pressure and elevation. Off by default.

## Requirements

- Python 3.11 or newer
- An MQTT broker publishing your station's data
- An atproto account and an **app password** (Settings → App Passwords)

## Quick start

```bash
git clone <this repo> && cd mqtt-atmowx-bridge
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env          # fill in ATP_IDENTIFIER and ATP_APP_PASSWORD
cp config.example.yaml config.yaml
```

Create your station record once — it is what every observation points at:

```bash
mqtt-atmowx-bridge station create \
  --name "Backyard" --lat 45.55 --lon -122.67 \
  --elevation 61 --timezone America/Los_Angeles
```

Put the AT-URI it prints into `STATION_URI` in your `.env`, describe your topics
in `config.yaml`, then check your work:

```bash
mqtt-atmowx-bridge validate            # shows how every mapping resolves
mqtt-atmowx-bridge run --dry-run       # builds real records, publishes nothing
mqtt-atmowx-bridge run
```

`--dry-run` is worth the two minutes: it logs the exact record it would have
published, so you can confirm your units and mappings against what the station
is really sending before anything reaches your repo.

## Configuration

`config.yaml` (see `config.example.yaml` for a fully commented version).
`${VAR}` reads from the environment and `${VAR:-fallback}` supplies a default, so
credentials stay out of the file.

### One JSON payload carrying the whole observation

```yaml
sources:
  - topic: "weather/station/loop"
    payload: json
    timestamp:
      path: dateutc
      format: epochMillis
    map:
      tempf:          { field: temperature,      unit: fahrenheit }
      humidity:       { field: relativeHumidity, unit: percent }
      baromabsin:     { field: pressureStation,  unit: inHg }
      windspeedmph:   { field: wind.speed,       unit: mph }
      winddir:        { field: wind.direction,   unit: degrees }
      dailyrainin:    { field: precipitation.day, unit: inches }
      solarradiation: { field: solarIrradiance,  unit: wattsPerSquareMeter }
      uv:             { field: uvIndex }
```

### One value per topic

```yaml
sources:
  - topic: "home/backyard/temperature"
    field: temperature
    unit: celsius

  - topic: "home/backyard/humidity"
    field: relativeHumidity
    unit: percent
```

Topics may use MQTT wildcards. A payload like `21.4 °C` needs `payload: text`,
which extracts the number.

### Calibration

Applied before unit conversion, so it operates on the raw value:

```yaml
  - topic: "sensors/probe/raw"
    field: temperature
    unit: celsius
    multiplier: 0.1     # sensor reports tenths
    offset: -0.4        # known bias
    ignoreAbove: 60     # obvious faults never reach a record
```

### When to publish

`interval` mode snapshots whatever readings are current on a timer:

```yaml
publish:
  mode: interval
  intervalSeconds: 300
  maxReadingAgeSeconds: 900   # older readings are left out entirely
```

`onMessage` publishes when a source you mark `trigger: true` reports, throttled
to `minIntervalSeconds`. Use it when one topic carries a complete observation and
you want records to line up with the station's own reporting cycle.

Readings from different topics accumulate, so a record can combine rain that
arrived ten minutes ago with a temperature from ten seconds ago — subject to
`maxReadingAgeSeconds`.

### Fields the lexicon does not cover

Prefix with `extra:` and it rides along in the record's `extra` array as a
`net.atmowx.defs#measurement`. Its dimension is inferred from the unit:

```yaml
      leafwetness: { field: "extra:leafWetness", unit: percent, decimals: 0 }
```

Run `mqtt-atmowx-bridge fields` for every publishable field and the unit spellings
each accepts.

### Derived values

```yaml
derive:
  dewPoint: whenMissing         # never | whenMissing | always
  pressureSeaLevel: whenMissing
  elevationMeters: 61           # required for the pressure reductions
```

`whenMissing` only fills a gap, so a station that reports its own dew point keeps
reporting it.

## Token management

The part most worth getting right, and the reason this is more than a `while
true` loop around an HTTP POST.

**App passwords, not your account password.** An app password is scoped and
revocable on its own. The bridge warns if what you gave it does not look like
one.

**Refreshes ahead of expiry.** Access tokens last a couple of hours. The session
is renewed `refreshSkewSeconds` (default 300) before expiry rather than waiting
for a rejection, so a publish is never delayed by a round trip that was always
going to fail.

**Handles refresh token rotation correctly.** `refreshSession` returns a *new*
refresh token and spends the old one. The new one is written to disk — atomically,
via a temp file and a rename — before the new access token is used. An
interrupted refresh cannot leave you holding a spent token, and a replayed one
looks like token theft to the PDS.

**Renewals are single-flight.** Concurrent publishes hitting an expired token
produce one refresh, not a race of rotations that invalidate each other.

**Falls back to a full login when it must**, with exponential backoff and jitter.
A rejected app password backs off to the ceiling rather than hammering the
endpoint; a rate-limited PDS is obeyed via `Retry-After`. A network blip during a
refresh does *not* spend the app password, since the refresh token is probably
still fine.

**Survives restarts.** The session is cached (`0600`) at `sessionFile`, so a
restart resumes rather than re-authenticating. Mount it on a volume in Docker.

**Writes to your real PDS.** Logging in at an entryway like `bsky.social` returns
a DID document pointing at the PDS that actually holds your repo; that is where
records go.

**Never logs a token.** Sensitive fields are redacted centrally, and the session
commands print claims (expiry, subject) rather than credentials.

Inspect the current state with `mqtt-atmowx-bridge session`.

## Reliability

Records that fail for a retryable reason (5xx, rate limits, network trouble) are
queued and replayed after the next success, oldest first, up to `queueSize`.
Because keys are deterministic, replaying is an overwrite rather than a
duplicate.

Records the server will never accept — a malformed record, a takedown — are
logged and dropped rather than blocking everything behind them.

MQTT reconnects automatically and resubscribes on every connect, so a broker
restart does not leave the bridge silently connected to nothing.

## Health endpoint

```yaml
health:
  enabled: true
  port: 8080
```

`/health` returns 200 while connected to MQTT and 503 otherwise. `/status`
returns the full picture: uptime, message counts, current readings, publish
counts, queue depth and session expiry.

## Commands

| Command | What it does |
| --- | --- |
| `run` | Run the bridge. `--dry-run` builds without publishing; `--once` publishes a single observation and exits |
| `validate` | Check the config and print how every mapping resolves |
| `station create` | Create a `net.atmowx.station` record and print its AT-URI |
| `station list` | List your station records |
| `login` | Authenticate and cache a session |
| `session` | Show the cached session's state |
| `logout` | Revoke the session and delete the cached file |
| `fields` | List publishable fields and the units each accepts |
| `convert` | Convert a value to its SI unit, to check a mapping |

## Docker

Pushes to `main` publish a multi-stage image to GitHub Container Registry
(Python 3.14):

```bash
docker pull ghcr.io/romkey/mqtt-atmowx-bridge:latest
docker run --rm -it \
  -v "$PWD/config.yaml:/config/config.yaml:ro" \
  -v mqtt-atmowx-bridge-data:/data \
  --env-file .env \
  ghcr.io/romkey/mqtt-atmowx-bridge:latest
```

For local builds, or if you prefer compose:

```bash
docker compose up -d
```

Mounts `config.yaml` read-only and keeps the session on a named volume so
restarts do not re-authenticate.

The published image is private to your account by default. To pull it on another
machine, sign in to GHCR with a token that has `read:packages`:

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

## Development

```bash
pip install -e ".[dev]"
pytest              # 220 tests
mypy                # strict
ruff format --check .
ruff check .
```

The end-to-end tests run a minimal in-process MQTT broker (`tests/broker.py`) so
the real paho client and the real publish path are exercised, with the PDS
stubbed at the HTTP transport.

CI runs the suite against Python 3.11 through 3.14 and builds the Docker image;
a separate Lint workflow runs `ruff format --check`, `ruff check`, and `mypy`.
Pushes to `main` (and version tags) publish the container image to GHCR. All three
workflows run on pushes to `main` and on pull requests where applicable.

## License

MIT
