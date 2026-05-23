# Postgres Capability-Boundary Demo (5 min)

This demo shows the **Charter Capability-Boundary** pattern in
action: a tiny asyncio TCP proxy sits in front of a real Postgres,
parses every SQL statement, checks it against a Charter, and
**refuses** queries that the Charter does not authorize. The check
is the same `aggregate_verdict` primitive the calling-agent-side
Delegation Gate uses — same trust order
(signature → JWKS → pin → lifecycle), same verdict semantics, just
enforced one layer closer to the resource.

**Status:** reference adapter, not a production database front.
Goal: prove the pattern is tractable so third parties can build
Stripe / S3 / arbitrary-tool adapters off the same template.

## What you need

- Docker (for `docker run postgres`), or a Postgres you can reach
- `pip install charter[postgres_proxy]` to pull in `sqlglot` (+ `asyncpg`
  if you want to drive the end-to-end test fixture)
- A signed Charter URL — for the demo we'll publish a tiny local one

## 1. Start Postgres

```bash
docker run --rm -d --name pg-demo \
  -e POSTGRES_PASSWORD=demo \
  -p 5432:5432 \
  postgres:16-alpine

# Seed a "public" table that the demo Charter allows reading from.
docker exec -i pg-demo psql -U postgres <<'SQL'
CREATE TABLE public_reports (id int primary key, label text);
INSERT INTO public_reports VALUES (1, 'ok'), (2, 'still ok');

CREATE TABLE production_secrets (id int primary key, value text);
INSERT INTO production_secrets VALUES (1, 'top-secret');
SQL
```

## 2. Publish a tiny demo Charter

If you already run `charter-server`, point the proxy at any Charter
URL you serve and skip this step. If not, the snippet below
generates an in-memory Charter that allows `SELECT` against
`public_reports` but refuses anything else:

```bash
python examples/postgres_gated_proxy/run_proxy.py
```

`run_proxy.py` is self-contained — it builds and signs a demo
Charter, serves it on `http://127.0.0.1:8765/charter`, and starts
the proxy on `127.0.0.1:55432` forwarding to `127.0.0.1:5432`.

## 3. Try queries through the proxy

Use any PG client. `psql` works because PG ignores unknown startup
parameters; we abuse that to pipe the `charter_url` through.

```bash
# Allowed: the demo Charter says reads from public_reports are fine.
PGOPTIONS="-c charter_url=http://127.0.0.1:8765/charter" \
  psql "postgresql://postgres:demo@127.0.0.1:55432/postgres" \
       -c "SELECT * FROM public_reports;"

# Refused: the demo Charter does NOT scope writes / drops.
PGOPTIONS="-c charter_url=http://127.0.0.1:8765/charter" \
  psql "postgresql://postgres:demo@127.0.0.1:55432/postgres" \
       -c "DROP TABLE production_secrets;"
# -> ERROR:  Charter proxy: verdict=incompatible ...
#    SQLSTATE: 42501 (insufficient_privilege)
```

`PGOPTIONS` works for `psql` because it converts every `-c key=value`
into a startup parameter (PG ignores ones it does not know, the
proxy reads `charter_url` out of them). For programmatic clients,
pass `server_settings={"charter_url": "..."}` to `asyncpg.connect`
or the equivalent for your driver.

## 4. Refusal without a charter_url

```bash
psql "postgresql://postgres:demo@127.0.0.1:55432/postgres" \
     -c "SELECT 1;"
# -> ERROR:  Charter proxy: charter_url required as a startup parameter; refusing.
```

This is the fail-closed default: the proxy never forwards anything
upstream until it has a Charter to evaluate against.

## What just happened

```
                       +-------------------------+
client --SELECT-->     | CharterGatedProxy       |  --SELECT--> Postgres
                       |  1. read startup        |              :5432
                       |  2. fetch + verify      |
                       |     Charter             |
                       |  3. intent_from_sql     |
                       |  4. aggregate_verdict   |
                       |     -> "allow"          |
                       +-------------------------+

client --DROP-->       | CharterGatedProxy       |  X  (never forwarded)
                       |  ...                    |
                       |  4. aggregate_verdict   |
                       |     -> "incompatible"   |
                       +--ErrorResponse 42501--> client
```

The proxy never sends the refused query upstream — so even if the
calling agent ignored the verdict, the database never sees the SQL.

## Where to go next

- Read the source: ~600 LOC total, split into `intent.py`
  (sqlglot-backed parsing), `gate.py` (Charter check projection),
  `proxy.py` (PG wire protocol + asyncio TCP).
- Inject your own grader (`hits_grader=`) to plug an LLM you already
  pay for instead of the conservative "all clauses hit" default.
- Use the pattern to build a Stripe gate (`amount + currency + payee`
  intent) or an S3 gate (`operation + bucket + prefix` intent).
- See [docs/architecture.md §5](../../docs/architecture.md) for how
  the resource-side gate composes with the calling-agent-side and
  edge-proxy gates (defense-in-depth).
