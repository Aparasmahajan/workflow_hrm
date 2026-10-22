# Project context & knowledge — HR-Orbit auto attendance

This is the full "brain" of the project: what it does, how it works, every API
detail we reverse-engineered, decisions made, current status, and how to debug.
Keep this file up to date — it's meant to survive without any chat history.

---

## 1. Goal
Automatically punch **in** and **out** on the HR-Orbit attendance portal
(`https://hr-orbit.azalio.io`) for the owner's own employee account, at a
**random time inside a window** on weekdays, using GitHub Actions (free, cloud,
no PC needed). Must be configurable: skip certain days, or target only specific
dates.

- Check-in window: **09:50–10:10 IST**
- Check-out window: **19:00–19:30 IST**
- Weekdays only (Mon–Fri); weekends auto-skipped.

> Note: this automates the owner's own attendance with their own credentials.
> Confirm it's acceptable under company policy — that's a human decision.

---

## 2. File map
```
workflow_hrm/
├── config.json                      # all knobs — edit this, not code
├── scripts/punch.py                 # login + punch; random wait; date gating
├── .github/workflows/check-in.yml   # schedule: 09:50 IST weekdays
├── .github/workflows/check-out.yml  # schedule: 19:00 IST weekdays
├── README.md                        # short setup steps
├── CONTEXT.md                       # THIS FILE — full knowledge
└── .gitignore
```

---

## 3. How "random time in a window" works
Cron cannot do random, and GitHub cron only fires at exact minutes. Solution:

1. The cron fires at the **start** of the window.
2. `punch.py` picks a random moment between "now" and the window end, then
   `time.sleep()`s until that moment before calling the API.
3. The GitHub job stays alive during the sleep (that's why `timeout-minutes` is
   set generously: 40 for check-in, 50 for check-out).

Edge handling: GitHub scheduled runs can be **delayed 5–15 min** under load. If
the job starts after the window has already begun, it picks a random time in the
remaining window. If it starts past the window end, there's a **15-minute grace**
(`GRACE` in punch.py) during which it still punches immediately; beyond that it
bails without punching.

---

## 4. Timezone math (critical)
GitHub cron is **always UTC**. India is **UTC+5:30** with **no daylight saving**,
so the offset never changes.

| Local (IST) | UTC   | Used for        |
|-------------|-------|-----------------|
| 09:50       | 04:20 | check-in cron   |
| 19:00       | 13:30 | check-out cron  |

Cron lines in the workflows:
- check-in:  `20 4 * * 1-5`  (04:20 UTC, Mon–Fri)
- check-out: `30 13 * * 1-5` (13:30 UTC, Mon–Fri)

The script itself does all time logic in `Asia/Kolkata` via Python `zoneinfo`.
On Windows (and to be safe on the runner) this needs the `tzdata` pip package —
that's why the workflows run `pip install requests tzdata`.

---

## 5. The API (reverse-engineered from the owner's browser curls)

Base URL: `https://hr-orbit.azalio.io/backend`

### 5.1 Login
- **POST** `/login`
- Body: **multipart/form-data** with fields `username` and `password`.
  (In `requests` this is `files={'username':(None,user),'password':(None,pwd)}`.)
- Returns a **JWT**. We do NOT know for certain which field holds it, so
  `punch.py` tries, in order: response-body keys `access_token`, `token`, `jwt`,
  `accessToken` (also under a nested `data` object), then falls back to the
  `jwt` **cookie** set on the response. If none match, it prints the full login
  body so the token key can be added to `login()`.

### 5.2 Check-in
- **POST** `/attendance/api/v1/addAttendance`
- Headers: `authorization: Bearer <JWT>`, `content-type: application/json`,
  and cookie `jwt=<JWT>` (the script sets both; `requests.Session` also carries
  the cookie automatically after login).
- JSON body:
  ```json
  {
    "date": "2026-10-16",
    "checkInTime": "09:59:16",
    "workLocation": "Office",
    "workOffice": "Azalio Technologies - Mohali"
  }
  ```
  `date` = today (IST), `checkInTime` = actual IST time at the punch moment.
  `workLocation` and `workOffice` come from config.

### 5.3 Check-out
- **PATCH** `/attendance/api/v1/addAttendance`  (same URL as check-in!)
- Same auth headers as above.
- JSON body:
  ```json
  { "date": "2026-10-15", "checkOutTime": "19:12:37" }
  ```

> History: the very first curls were truncated and we guessed `/in` and
> `/attendance`. The owner later pasted the real endpoints — both actions use
> `/attendance/api/v1/addAttendance`, distinguished only by HTTP method (POST vs
> PATCH) and body. Config and code were updated to match.

### 5.4 refresh_token API (NOT used)
There is also a `/refresh_token` endpoint, but we don't need it: the script logs
in fresh on every run, so it always has a valid access token. Ignore it.

---

## 6. config.json reference
| Key | Meaning |
|-----|---------|
| `enabled` | master on/off switch for both workflows |
| `timezone` | `Asia/Kolkata` |
| `base_url` | `https://hr-orbit.azalio.io/backend` |
| `endpoints.login/checkin/checkout` | `{path, method}` for each call |
| `work_location` | goes into check-in body as `workLocation` (currently `"Office"`) |
| `work_office` | goes into check-in body as `workOffice` (the office name) |
| `skip_weekends` | auto-skip Sat/Sun (true) |
| `skip_dates` | list like `["2026-10-24"]` — never run on these (leaves/holidays) |
| `only_dates` | if NON-empty, run ONLY on these dates (ignores weekends). Use to target specific days |
| `checkin_window` / `checkout_window` | `{start, end}` in IST `HH:MM` |

Gating precedence in `should_run_today()`:
1. `enabled=false` → skip.
2. `only_dates` non-empty → run only if today is listed (else skip), weekend ignored.
3. else: skip if today in `skip_dates`; skip if weekend and `skip_weekends`.

---

## 7. punch.py — command-line flags
```
python scripts/punch.py --action in|out        # the real thing
                        [--config config.json]
                        [--dry-run]     # plan only, never calls API
                        [--no-sleep]    # skip the random wait, punch now (testing)
                        [--login-only]  # just log in to verify creds; no punch
```
The workflows expose `dry_run`, `no_sleep`, `login_only` as **workflow_dispatch**
inputs (manual "Run workflow" button). Scheduled runs use none of them.

---

## 8. Secrets (IMPORTANT gotcha)
The script reads two env vars: `HRORBIT_USERNAME`, `HRORBIT_PASSWORD`.
On GitHub these come from secrets referenced as `${{ secrets.HRORBIT_* }}`.

**They must be _Repository_ secrets, not _Environment_ secrets.** The workflows do
not declare an `environment:`, so environment-scoped secrets are invisible to
them. (We hit this exact issue: secrets were first added under an environment
named "workin" and the job couldn't read them. Fixed by adding them under
Settings ▸ Secrets and variables ▸ Actions ▸ **Repository secrets**.)

Never commit the password or any JWT to the repo. `.gitignore` blocks common
secret filenames. The token is masked in logs (`mask()`).

---

## 9. Deploying / updating the repo
`git push` wasn't working on the owner's machine, so files are uploaded via the
**GitHub web UI** (Add file ▸ Upload files, drag the folders; or Create new file
and type `.github/workflows/check-in.yml` to auto-create folders).

The file paths MUST be exact — workflows only run when at
`.github/workflows/*.yml`. After uploading, confirm the Actions tab lists
"Check-in" and "Check-out".

> TODO (nice to have): fix `git push` (usually an auth/remote issue) so config
> tweaks don't require re-dragging files. Ask what error `git push` shows.

---

## 10. Testing recipe
1. **Local login check** (fastest, private — run in your own terminal):
   ```powershell
   $env:HRORBIT_USERNAME = "paras.mahajan@azalio.io"
   $env:HRORBIT_PASSWORD = "your-password"
   py scripts\punch.py --action in --login-only
   ```
   Needs `pip install requests tzdata` once.
2. **GitHub login check**: Actions ▸ Check-in ▸ Run workflow ▸ `login_only=true`.
3. **Real immediate punch** (only if not already punched today):
   `no_sleep=true`, `login_only=false`.
4. Expand the **"Punch in"** step log (or ⚙ ▸ View raw logs) to read output.

What good output looks like:
```
login -> HTTP 200
token source: ...
got token ab12cd34…xয (len=…)
✅ LOGIN OK            # for --login-only
# or
✅ IN done at 09:59:12 IST
```

---

## 11. Current status (as of 2026-10-22)
- ✅ Files written and logic unit-tested locally (gating, window/random-sleep,
  only_dates all behave correctly).
- ✅ Endpoints corrected to the real `/attendance/api/v1/addAttendance`.
- ✅ Uploaded to GitHub repo `Aparasmahajan/workflow_hrm`.
- ✅ Repository secrets `HRORBIT_USERNAME` / `HRORBIT_PASSWORD` set.
- ✅ First manual **Check-in** run (#1) succeeded (all steps green) — plumbing
  (secrets, Python, deps, no crash) confirmed working.
- ⬜ **Not yet confirmed from the run log** whether login returned a token and
  which branch ran (login-only vs real punch vs window-missed bail). Need to
  read the "Punch in" step output.
- ⬜ `--login-only` flag + `login_only` input were added locally AFTER the first
  run; re-upload `scripts/punch.py` and `.github/workflows/check-in.yml` to use
  them on GitHub.
- ⬜ check-out never run yet.
- ⬜ `git push` still broken (using web upload meanwhile).

---

## 12. Troubleshooting
| Symptom | Likely cause / fix |
|---------|--------------------|
| `Missing HRORBIT_USERNAME / HRORBIT_PASSWORD` | Secrets not set, or set as Environment (not Repository) secrets. |
| `Could NOT find a token` + body printed | Login response uses a different token field — add its key to the list in `login()`, or confirm login actually succeeded. |
| Step red at `login -> HTTP 401/400` | Wrong username/password, or login body format changed. |
| `HTTP 404/405` on punch | Endpoint path/method wrong in config — re-check DevTools. |
| Workflow not in Actions tab | Files not at exact path `.github/workflows/*.yml`. |
| Scheduled run didn't fire on time | GitHub best-effort delay (normal); 15-min grace covers small delays. Schedules also only run from the default branch. |
| Duplicate punch | Don't run a real punch if already checked in; use `--login-only`. No idempotency guard yet (see below). |

---

## 13. Ideas / TODO backlog
- **Idempotency**: before punching, GET today's attendance and skip if already
  done, to avoid duplicates. (Need the "get attendance" endpoint.)
- Fix `git push` auth so updates don't need web upload.
- Optional: notify on failure (e.g., the run already emails on workflow failure
  by default if GitHub notifications are on).
- Optional: a tiny holiday list auto-loaded into `skip_dates`.

---

## 14. Key facts to remember
- Owner/account: Paras Mahajan — `paras.mahajan@azalio.io`, Azalio Technologies,
  Mohali office. (This Claude session's user email is adarsh.gowni@azalio.io.)
- Repo: `github.com/Aparasmahajan/workflow_hrm` (keep it **private**).
- Everything time-related is IST; GitHub cron is UTC (+5:30 difference).
- Both punch actions share one URL; method is what differs (POST=in, PATCH=out).
