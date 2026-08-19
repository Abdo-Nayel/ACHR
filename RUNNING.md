# Running the project locally

Verified working: Django 5.2.17, PostgreSQL 16.13, Redis 7, Python 3.11.

---

## 0. What you actually need

**PostgreSQL 16+. That is the only required service.**

Redis is optional locally — leave `USE_REDIS` unset and the cache falls back
to in-memory while Celery tasks run inline. Every endpoint, the ledger and
payroll all work without it. Turn it on when you are working on caching,
throttling or background workers.

There is no supported native Redis build for Windows, so making it mandatory
would mean forcing Docker or WSL on you to run a single-process dev server.
Not worth it.

## 1a. No Docker — install PostgreSQL directly (simplest on Windows)

1. Download the PostgreSQL 16 installer from
   <https://www.postgresql.org/download/windows/>.
2. During setup, set a password for the `postgres` user and **remember it**.
   Keep the default port `5432`.
3. Open **SQL Shell (psql)** from the Start menu, press Enter through the
   prompts, type the password, then:

   ```sql
   CREATE DATABASE erp;
   ```

4. In `backend\.env` set:

   ```ini
   POSTGRES_APP_USER=postgres
   POSTGRES_APP_PASSWORD=<the password you chose>
   ```

That is enough to run. See §2 to switch to the non-superuser role before you
rely on tenant isolation.

## 1. The fast path — Docker (if you have it)

From `E:\Accouting - HR`:

```powershell
docker compose up -d db          # redis is optional; add it if USE_REDIS=1
```

Then, in `backend\`:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements\dev.txt

copy .env.example .env      # then edit if your ports differ

python manage.py migrate
python manage.py seed_permissions
python manage.py seed_demo_tenant
python manage.py runserver
```

Open <http://127.0.0.1:8000/api/schema/docs/> for Swagger.

## 2. Native PostgreSQL instead of Docker

Create the database and the **non-superuser** application role. This is not
optional hardening — it is the isolation model. PostgreSQL skips every
row-level security policy for a role with `rolsuper` or `rolbypassrls`, so
running the app as `postgres` leaves all 87 policies installed and none of
them in effect. The ORM keeps filtering, so the UI looks correct and the
tenant tests pass; the first evidence is one customer reading another's ledger
through a `.raw()` query or an unbound Celery task.

Create the database, then let the app create and verify the role:

```sql
CREATE DATABASE erp;
```

```powershell
# .env still points at a superuser at this point — this connection needs
# CREATEROLE and GRANT rights.
python manage.py provision_db_roles
```

It is idempotent, and it finishes by asserting that the role it created has
neither `SUPERUSER` nor `BYPASSRLS` — so a run that reports success is proof,
not a claim. Then set in `backend\.env`:

```ini
POSTGRES_APP_USER=erp_app
POSTGRES_APP_PASSWORD=erp_app
```

Migrations need DDL rights, so run them as the owner and serve as `erp_app`:

```powershell
$env:POSTGRES_APP_USER="postgres"; python manage.py migrate
Remove-Item Env:\POSTGRES_APP_USER
python manage.py runserver
```

Under Docker this is done for you: `infra/postgres/init/10-roles.sql` runs on
first boot and creates `erp_migrator` (owner) and `erp_app` (runtime).

To confirm isolation is actually on, run the tenant suite — it must run as
`erp_app`, because a suite running as a superuser proves nothing about RLS:

```powershell
pytest tests\test_tenant_isolation.py
```

## 3. `.env`

```ini
DJANGO_SETTINGS_MODULE=config.settings.dev
DJANGO_SECRET_KEY=dev-only-not-a-secret
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,0.0.0.0,testserver

POSTGRES_DB=erp
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_APP_USER=postgres
POSTGRES_APP_PASSWORD=change-me

# Optional. Unset = no Redis needed; in-memory cache and inline tasks.
# USE_REDIS=1
# REDIS_URL=redis://127.0.0.1:6379/0
```

### If `pip install` fails

Read the **first** error, not the last. A dependency conflict makes pip install
nothing at all, and the `ModuleNotFoundError: No module named 'django'` that
follows on every subsequent command is a symptom, not the cause.

Verify the install landed before moving on:

```powershell
python -c "import django; print(django.get_version())"
```

That must print `5.2.x`. If it errors, the venv is empty — fix the install
first; `migrate` cannot work.

Optional extras (PDF rendering, WebSockets, the Celery dashboard) are
deliberately **not** in `base.txt`; they pull native toolchains that fail on
Windows without extra setup and none of them are needed to run the API:

```powershell
pip install -r requirements\optional.txt   # only when you need them
```

## 4. Background workers (optional)

```powershell
celery -A config worker -l info -Q default,payments,reports,notifications
celery -A config worker -l info -Q payroll --concurrency=2
celery -A config beat   -l info
```

Payroll gets its own worker and a low concurrency on purpose: a payroll run
posts to the general ledger, and two workers processing the same run
concurrently is the one race the idempotency key exists to stop.

## 5. Demo credentials

`seed_demo_tenant` prints these. Password for all three:
`demo-password-not-for-production`

| Login | Role | What they can see |
|---|---|---|
| `owner@<slug>.example.com` | Owner | Everything |
| `accountant@<slug>.example.com` | Accountant (+ dept manager, scoped) | Ledger, invoices, expenses — **not** payslips |
| `employee@<slug>.example.com` | Employee | Only their own payslip, leave and attendance |

The seed creates a tenant with a 39-account chart, 6 journals, a fiscal year
with 12 periods, 3 employees in a 3-level department tree, a customer, items,
stock, an issued invoice with a part payment, an expense, and a calculated
payroll run.

## 6. Health checks

```
GET /healthz   liveness  — never touches the DB
GET /readyz    readiness — DB, cache, and asserts RLS + ledger triggers exist
GET /version
```

A healthy `/readyz` looks like:

```json
{"status":"ready","checks":{"database":"ok","cache":"ok",
 "rls_tables":86,"ledger_triggers":5,"guards":"ok"}}
```

If `rls_tables` is 0 or `ledger_triggers` < 5, the process refuses readiness.
Serving traffic against a database where someone dropped a policy is worse
than being down.

## 7. Smoke test

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login/ \
  -H 'Content-Type: application/json' \
  -d '{"email":"owner@<slug>.example.com","password":"demo-password-not-for-production"}'
```

Returns `access`, `refresh`, the active `tenant`, and every tenant the user
belongs to. Then:

```bash
curl -s -H "Authorization: Bearer $ACCESS" http://127.0.0.1:8000/api/v1/invoices/
```

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every list endpoint returns `200` with `results: []` | The tenant was not bound, or a queryset was frozen at import time | Both are fixed in this build. If it recurs, check `/readyz` and that your viewset does not cache `queryset` outside `get_queryset()` |
| `permission_denied` right after changing a role | Effective permissions are cached for the TTL | `redis-cli FLUSHALL`, or call the invalidation hook |
| `This account is not an active member of any organisation` | Membership rows are RLS-protected and the lookup ran without the scoped bypass | Fixed; see `cross_tenant_lookup()` in `apps/core/tenancy_context.py` |
| `new row violates row-level security policy` | Writing a tenant row with no tenant bound | Wrap in `tenant_context()`, or `cross_tenant_lookup()` for genuinely pre-tenant writes |
| `No default throttle rate set for 'login'` | `DEFAULT_THROTTLE_RATES` emptied while a view still names a scope | Fixed in `config/settings/dev.py` |
| `ResolutionImpossible` on `pip install -r requirements/base.txt`, then `ModuleNotFoundError: No module named 'django'` on every command after it | `django-celery-beat` 2.6.x declares `Django<5.1`, which contradicts our `Django>=5.1` floor. pip installs **nothing**, so the venv stays empty and the real error scrolls past | Fixed: the floor is now `django-celery-beat>=2.7`. Re-run `pip install -r requirements\dev.txt` |
| `pip install` fails building WeasyPrint / Daphne on Windows | Those need native GTK / Twisted toolchains | They moved to `requirements/optional.txt` and are not needed to run the API |
| `TypeError: ... pool_class_kwargs` | Redis pool options nested one level too deep | Fixed in `config/settings/base.py` |
| `psycopg.errors.ConnectionTimeout: connection timeout expired` | PostgreSQL is not running or not reachable on the configured host/port. **Timeout** (rather than *refused*) usually means a firewall or a container whose port was never published | Check `netstat -ano \| findstr :5432` returns a line. If empty, nothing is listening — start PostgreSQL, or install it per §1a |
| `'docker' is not recognized` | Docker is not installed | You do not need it. Follow §1a and install PostgreSQL directly |
| `Connection refused` to Redis on startup | Redis is not running | Leave `USE_REDIS` unset in `.env`; Redis is optional locally |

## 9. What is real and what is scaffolding

**Real and exercised:** the ledger and posting engine, the payroll engine,
tenant isolation (ORM + RLS), RBAC/ABAC, JWT auth with tenant claims and
tenant switching, the error envelope, pagination, health checks, and the
seeds.

**Scaffolding:** the 10 module `urls.py` files are read-only placeholder
routes so the router mounts. Write paths and state-transition actions
(`POST /invoices/{id}/issue`, `POST /payroll-runs/{id}/approve`, the webhook
receiver) are specified in `docs/06-api-contract.md` but not yet wired to
their services. The services themselves exist and are tested.
