# hydrawise-report

A dependency-free Python client for [Hydrawise](https://www.hydrawise.com/) (Hunter
Industries) irrigation controllers, plus the piece Hydrawise itself does not
provide: **a monthly water and electricity report per person**, emailed to each
valve's owner.

* Talk to the controller — list zones, see what is watering, run/stop/suspend a valve.
* Keep a local run log, because the public API has no watering history.
* Turn logged run time into cubic metres, kWh and money, split by whoever owns each valve.
* Mail each person their own report, in English or Arabic.

No runtime dependencies: standard library only (`urllib`, `sqlite3`, `smtplib`).
Tests are `unittest` and never touch the network.

---

## 1. Get an API key — not a password

Everything here authenticates with a Hydrawise **API key**, which you get from
the Hydrawise web app:

> app.hydrawise.com → **My Account** → **Account Details** → API key

```bash
export HYDRAWISE_API_KEY='your-key-here'
```

The key is a bearer credential in query-string clothing: anyone holding it can
water your garden. Keep it in the environment (or a secrets manager), never in
git. This tool never asks for, stores, or transmits your Hydrawise account
password — if you have shared that password anywhere, change it and use an API
key instead.

## 2. Install

```bash
git clone <this repo> && cd hydrawise-report
pip install -e .          # gives you the `hydrawise` command
# or run it straight from the checkout:
python -m hydrawise --help
```

Python 3.9 or newer.

## 3. Look around

```bash
hydrawise controllers            # controllers on the account
hydrawise status                 # zones, next runs, what is watering now
hydrawise zones --json           # raw payload, for scripting
hydrawise run "Front lawn" --minutes 10
hydrawise stop --all
hydrawise suspend 2 --days 3     # by zone number
hydrawise resume 2
```

Zones can be named by **number** (the position on the controller), by **relay
id**, or by **name** (`"palms"` matches *Date palms*).

## 4. Configure the site

```bash
hydrawise init-config            # writes hydrawise.config.json
```

Then fill it in — this is where the numbers the controller cannot know live:

```jsonc
{
  "timezone": "Asia/Riyadh",
  "currency": "SAR",
  "water":       { "tariff_per_m3": 3.0 },
  "electricity": { "tariff_per_kwh": 0.18, "default_pump_kw": 2.2 },

  "people": [
    { "id": "ahmed", "name": "Ahmed", "email": "ahmed@example.com", "language": "ar" }
  ],
  "zones": [
    { "zone": 1, "name": "Front lawn", "flow_rate_lpm": 40.0, "pump_kw": 2.2, "owner": "ahmed" }
  ],

  "email": {
    "smtp_host": "smtp.gmail.com", "smtp_port": 587,
    "username": "you@example.com", "from_address": "you@example.com",
    "password_env": "HYDRAWISE_SMTP_PASSWORD"
  }
}
```

`flow_rate_lpm` is the valve's flow in **litres per minute**. Take it from the
nozzle/emitter chart, or measure it once: run the zone for 60 seconds into a
graduated bucket. Without it the report still shows the zone's run hours, but
its water column reads `—` instead of a fabricated number.

Secrets are referenced by environment-variable **name** (`api_key_env`,
`password_env`) and read at run time, so the config file itself stays safe to
keep next to the code. `.gitignore` excludes `hydrawise.config.json` and `*.db`
anyway.

## 5. Record the watering

The Hydrawise REST API v1 answers *what is watering now*; it has no history
endpoint. So run the poller and let it build the log:

```bash
hydrawise poll --interval 60        # foreground, Ctrl-C to stop
hydrawise poll --once               # a single sample, e.g. from cron
hydrawise runs --month 2026-08      # what the log holds
```

Each poll compares the controller's `running` list against the open runs in
`hydrawise.db` and opens, extends or closes them. A poller that starts
mid-run back-dates the start from the zone's remaining time, and one that dies
mid-run has that run closed at its programmed length rather than left open.

As a systemd service:

```ini
# /etc/systemd/system/hydrawise-poll.service
[Unit]
Description=Hydrawise run logger
After=network-online.target

[Service]
Environment=HYDRAWISE_API_KEY=your-key-here
WorkingDirectory=/opt/hydrawise
ExecStart=/opt/hydrawise/.venv/bin/hydrawise poll --interval 60 --quiet
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

The client throttles itself — it spaces requests out and honours the server's
own `nextpoll` hint — and backs off rather than dying when the API answers
HTTP 429. A 60-second interval is plenty: the shortest run a zone can be given
is one minute.

## 6. Report and bill

```bash
hydrawise report                                   # last month, per person
hydrawise report --month 2026-08 --format csv --output august.csv
hydrawise report --month 2026-08 --format json     # for a dashboard
hydrawise report --month 2026-08 --format html --person ahmed   # one mail body

hydrawise send-reports --month 2026-08 --dry-run   # compose, do not send
export HYDRAWISE_SMTP_PASSWORD='app-password'
hydrawise send-reports --month 2026-08 --skip-empty
```

A monthly send on the 1st, via cron:

```cron
0 6 1 * * cd /opt/hydrawise && HYDRAWISE_API_KEY=... HYDRAWISE_SMTP_PASSWORD=... \
          .venv/bin/hydrawise send-reports --skip-empty >> /var/log/hydrawise-mail.log 2>&1
```

With no `--month`, both commands use **last month**, evaluated in the config's
timezone.

### How the numbers are worked out

```
water   m³  = flow_rate_lpm × run_minutes ÷ 1000
energy kWh  = pump_kw       × run_hours
cost        = m³ × tariff_per_m3  +  kWh × tariff_per_kwh
```

Both conversions are **estimates from run time**, and the report says so in
every mail. What that means in practice:

* Accuracy is the accuracy of your `flow_rate_lpm`. A bucket test per zone
  costs ten minutes and makes the whole report defensible.
* If a zone has no flow rate, its water is not counted (and the report warns).
* Electricity is attributed to whoever's valve was open — the pump runs for
  their zone, so its kWh is theirs. Zones on mains pressure with no pump should
  set `"pump_kw": 0`.
* A run is billed to the month it **started** in; runs are not split across the
  month boundary.
* If your controller has a **flow meter**, its measured totals live in the
  Hydrawise app's own reports and will be more accurate than this estimate —
  worth cross-checking once against a month of this report.

## 7. Use it as a library

```python
from hydrawise import HydrawiseClient, RunStore, build_report, month_bounds
from hydrawise.config import Config

client = HydrawiseClient(api_key)
status = client.status_schedule()
for zone in status.zones:
    print(zone.number, zone.name, zone.nice_time, zone.is_scheduled)

client.run_zone(status.zone("Front lawn").relay_id, seconds=600)

config = Config.load("hydrawise.config.json")
start, end = month_bounds("2026-08", config.timezone)
with RunStore("hydrawise.db") as store:
    report = build_report(store.runs_between(start, end), config,
                          period="2026-08", start=start, end=end)
print(report.person("ahmed").cubic_meters)
```

Every model keeps the payload it was parsed from in `.raw`, so a field this
package does not model yet is still reachable.

## 8. API coverage

The Hydrawise REST API v1 is three GET endpoints, all authenticated with
`api_key`:

| Endpoint | Wrapped by | Notes |
|---|---|---|
| `customerdetails.php` | `customer_details()` | account + controllers |
| `statusschedule.php` | `status_schedule()` | zones, sensors, live runs, `nextpoll` |
| `setzone.php` | `run_zone`, `run_all_zones`, `stop_zone`, `stop_all_zones`, `suspend_zone`, `suspend_all_zones`, `resume_zone`, `resume_all_zones` | `period_id=999` + `custom=<seconds\|epoch>` |

Errors arrive as HTTP 200 with an `error_msg` field as often as they do as an HTTP
status, so both are classified into `HydrawiseAuthError`,
`HydrawiseRateLimitError` and `HydrawiseAPIError`. API keys are redacted from
error messages.

## 9. Tests

```bash
python -m unittest discover -s tests -t .
```

118 tests, no dependencies and no internet access: HTTP is a scripted fake
transport and time is a fake clock, so rate limiting, caching and back-off are
asserted deterministically. Four of them run the real `urllib` transport
against a throwaway server on loopback.

## 10. Layout

```
hydrawise/
  client.py    REST API v1 client: throttling, retries, error classification
  models.py    typed views over the API's loose JSON
  storage.py   SQLite run log built from repeated polls
  poller.py    the polling loop
  config.py    site config: valves, flow rates, people, tariffs, SMTP
  usage.py     run time → m³, kWh, cost, per person
  report.py    text / CSV / JSON / HTML rendering (en + ar)
  mailer.py    SMTP delivery
  cli.py       the `hydrawise` command
tests/         118 unittest tests
```

Arabic documentation: [`docs/README.ar.md`](docs/README.ar.md).

## License

MIT.
