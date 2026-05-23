"""Charter reference adapter — Postgres capability-boundary proxy.

This subpackage demonstrates the *Capability-Boundary Enforcement*
pattern (PRODUCT.md §5.6 / docs/decisions.md ADR-006). Where the
existing OpenAI Agents / AP2 adapters install Charter as a
*Delegation Gate* on the calling-agent side (voluntary, cooperative),
this adapter installs Charter on the **resource side**, in front of a
real Postgres database, and **refuses** SQL that the Charter does not
authorize.

The proxy is a teaching reference, not a production database front:
it covers the minimum slice of the Postgres wire protocol needed to
sniff client SQL (``Q`` Simple Query and ``P`` Parse messages),
transparently forwards everything else, and never re-implements PG
features such as connection pooling, prepared-statement caching,
SSL/TLS, or replication routing. The goal is to ship the *pattern*
so third parties can build adapters for Stripe / S3 / arbitrary tool
runtimes.

Security stance
---------------

**Fail-closed everywhere.** Any SQL the adapter cannot parse, any
Charter that cannot be fetched, any grader that raises — every one of
these collapses to ``verdict.decision == "incompatible"`` and the
client receives a PG ``ErrorResponse`` with SQLSTATE 42501
(``insufficient_privilege``). The proxy never silently forwards a
query it could not evaluate.

Public surface
--------------

  - :class:`CharterGatedProxy` — the asyncio TCP server that listens
    for PG clients, gates each SQL statement against a Charter, and
    forwards approved statements to the upstream Postgres.
  - :func:`intent_from_sql` — the SQL-to-intent extractor, exposed for
    callers who want to reuse the parsing logic outside the proxy
    (e.g. for one-shot policy checks).
"""

from __future__ import annotations

from .intent import SqlIntent, intent_from_sql
from .proxy import CharterGatedProxy

__all__ = [
    "CharterGatedProxy",
    "SqlIntent",
    "intent_from_sql",
]
