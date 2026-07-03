# Deploying Bugalizer on the Windows LAN box

Target setup (Phase 5 §5.5): a Windows 10/11 machine with an NVIDIA GPU
(RTX 4070 Super) hosting Bugalizer permanently on the LAN so other apps can
submit bug reports. **Ollama runs natively on Windows** (direct GPU access);
Bugalizer runs as a supervised service pointing at it.

```
other LAN apps ──HTTP──▶ Bugalizer :8090 ──▶ Ollama :11434 (native, GPU)
                             │                    (Stages 2–3)
                             └──────────────────▶ Anthropic API (Stage 4)
```

## 1. Prerequisites

1. **Ollama for Windows** — install from <https://ollama.com/download/windows>,
   then pull the pipeline model:

   ```powershell
   ollama pull qwen2.5-coder:7b
   ```

   Verify it serves: open <http://localhost:11434/api/tags> — you should see
   the model listed. Ollama installs itself to start with Windows by default;
   confirm in Task Manager ▸ Startup apps.

2. **Git for Windows** (native deploys only; the Docker image bundles git) —
   needed by the repo clone/pull pipeline stage.

## 2. Configuration (`.env`)

```powershell
copy .env.example .env
```

Edit `.env` and set at minimum:

- `BUGALIZER_API_KEYS` — **required for any LAN deployment.** Generate one:

  ```powershell
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

  (Multiple keys are comma-separated — hand each client app its own key so
  they can be revoked independently.)

- `BUGALIZER_ANTHROPIC_API_KEY` — required for Stage 4 fix proposals.

Everything else has sane LAN defaults (see the comments in `.env.example`).
The app reads `.env` from its working directory on startup; real environment
variables override it.

## 3. Option A — Docker Desktop (recommended)

1. Install Docker Desktop and enable **Settings ▸ General ▸ "Start Docker
   Desktop when you sign in"** (this is what makes `restart: unless-stopped`
   survive a reboot).
2. From the repo directory:

   ```powershell
   docker compose up -d --build
   ```

What the compose file wires for you:

- `BUGALIZER_OLLAMA_HOST=http://host.docker.internal:11434` — the container
  reaches the host's native Ollama.
- `./data` on the host holds **all** mutable state (`bugalizer.db`, cloned
  `repos/`, repo-map `cache/`) — one directory to back up.
- `restart: unless-stopped` + a container healthcheck against `/health/live`.

Update procedure: `git pull`, then `docker compose up -d --build`.

## 4. Option B — native service via NSSM (no Docker)

1. Install [uv](https://docs.astral.sh/uv/) and run `uv sync` (no `--dev`)
   in the repo directory.
2. Install [NSSM](https://nssm.cc/) and register the service (adjust paths):

   ```powershell
   nssm install Bugalizer "C:\Users\<you>\.local\bin\uv.exe" ^
     "run --no-dev uvicorn bugalizer.main:app --host 0.0.0.0 --port 8090"
   nssm set Bugalizer AppDirectory "C:\path\to\bugalizer"
   nssm set Bugalizer AppStdout "C:\path\to\bugalizer\logs\bugalizer.log"
   nssm set Bugalizer AppStderr "C:\path\to\bugalizer\logs\bugalizer.log"
   nssm set Bugalizer AppRotateFiles 1
   nssm start Bugalizer
   ```

   `AppDirectory` matters: the app reads `.env` and resolves the relative
   `bugalizer.db` / `./repos` / `./cache` paths from there. NSSM restarts the
   process if it dies and starts it at boot.

   *Task Scheduler fallback* (no NSSM): create a task triggered **At startup**,
   action `uv.exe` with the same arguments and *Start in* set to the repo
   directory; enable "Restart the task if it fails".

## 5. Open the firewall port

```powershell
netsh advfirewall firewall add rule name="Bugalizer 8090" dir=in action=allow protocol=TCP localport=8090
```

(Scope it to the private profile / your subnet if the machine ever leaves the
home LAN.)

## 6. Verify

- Liveness: `http://<lan-host>:8090/health/live` → `{"status": "ok", ...}`
- Readiness: `http://<lan-host>:8090/health` → `checks.database` and
  `checks.ollama` both `true`
- Dashboard: open `http://<lan-host>:8090/`, paste an API key in the
  top-right box (stored in the browser's localStorage).

Then run the full end-to-end check: see [`smoke-test.md`](smoke-test.md).

## 7. How other LAN apps submit bugs

Register the project once (id comes back in the response), clone its repo,
then POST reports:

```bash
HOST=http://192.168.68.xx:8090
KEY=<api key>

# one-time project setup
curl -s -X POST $HOST/api/v1/projects -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name": "myapp", "repo_url": "https://github.com/you/myapp.git"}'
curl -s -X POST $HOST/api/v1/projects/<project_id>/clone -H "X-API-Key: $KEY"

# submit a bug (analysis_mode optional: auto | local_only | hold)
curl -s -X POST $HOST/api/v1/reports -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{
        "title": "Login button does nothing on Safari",
        "description": "Clicking Login has no effect; console shows a TypeError in auth.js.",
        "reporter": "qaagent",
        "project_id": "<project_id>",
        "severity": "high",
        "analysis_mode": "auto"
      }'
```

Full API reference: `http://<lan-host>:8090/docs`.

## 8. Backups

All state is one SQLite file plus re-creatable caches. Either:

- **Cold copy** — stop the service (`docker compose stop` / `nssm stop
  Bugalizer`), copy `bugalizer.db` (plus `-wal`/`-shm` siblings if present),
  restart; or
- **Live backup** — no downtime, safe under WAL:

  ```powershell
  sqlite3 data\bugalizer.db ".backup data\bugalizer-backup.db"
  ```

`repos/` and `cache/` need no backup — they are re-cloned/re-built on demand.
