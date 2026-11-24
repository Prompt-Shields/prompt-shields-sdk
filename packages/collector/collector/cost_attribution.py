"""Cost attribution: teams, projects, calling services and cost tags (#347).

Every usage event can be attributed to a team and a project of its tenant
(principals, teams and projects, #338), to the service that made the call,
and to free-form cost tags:

* ``team`` / ``project`` on the ingest event (the gateway sends its
  ``X-PS-Team`` / ``X-PS-Project`` request headers, the SDK its
  ``ps_metadata``): a team or project id, or its exact name. They are
  resolved at ingest against the tenant's own teams and projects
  (`resolve_refs`); an unknown reference leaves the event unattributed (it
  is never stored as sent). A project belongs to one team, so a resolved
  project fixes the event's team, whatever team was declared (org > team >
  project). The ids are stamped on the event, so moving a project to
  another team re-attributes future spend only.
* ``calling_service`` (``X-PS-Service`` at the gateway): stored on the
  event as sent (it was already kept on the asset).
* ``tags`` (``X-PS-Tags`` at the gateway: ``key=value,key=value``; the SDK
  may also send an object or a list of ``key=value`` strings): at most
  `MAX_TAGS` tags (`parse_tags`). Keys are lower-cased identifiers
  (`TAG_KEY_RE`); keys starting with ``ps:`` are reserved for Prompt Shields
  (the FOCUS export's registry tags) and dropped; values are short
  printable tokens (`TAG_VALUE_RE`). Invalid tags are dropped one by one.

Raw events keep every valid tag (and are purged at raw retention). The usage
rollups, which are kept about 25 months, keep only the tags whose keys the
tenant lists in ``settings.cost_tag_keys`` (at most `MAX_ROLLUP_TAG_KEYS`
keys, set by the operator tenants API; none by default), so a tag with an
unbounded set of values (a request id) cannot grow the rollups like raw
events. ``group_by=tag:<key>`` on the usage series and showback needs the
key in that list. Changing the list applies to buckets recomputed after
the change; ``python -m collector.usage_rollups rebuild`` recomputes the
ones still in raw retention.

Credentials do not carry a team yet: when virtual keys (#343) do, their
team, project and default tags win over the declared ones.

Showback (`showback`) sums one UTC month of the daily rollups per team,
project, user (id hash), calling service or cost tag. Spend no group
claims is reported as ``unallocated`` (never dropped), and the groups'
cents are allocated with the largest-remainder method, so the groups plus
unallocated add up to the tenant's total for the month to the cent.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from db.models import Project, Team, UsageRollupDaily
from sqlalchemy import Text, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

MAX_TAGS = 20
TAG_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_.:/-]{0,62}")
# No "," or "=": they separate tags in the header form.
TAG_VALUE_RE = re.compile(r"[A-Za-z0-9_.:/@+ -]{1,128}")
RESERVED_TAG_PREFIX = "ps:"
# The header form is at most this long; longer is dropped as a whole.
MAX_TAGS_HEADER_LENGTH = 4096

# Team and project references: an id, or a name as collector/rbac_admin.py
# allows it.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,99}")

SETTINGS_KEY = "cost_tag_keys"
MAX_ROLLUP_TAG_KEYS = 10

TAG_GROUP_PREFIX = "tag:"
UNALLOCATED = "unallocated"
CENT = Decimal("0.01")


# ──────────────────────────────────────────────────────────────────────
# Tags and references
# ──────────────────────────────────────────────────────────────────────


def _tag_pairs(value: Any) -> Iterable[tuple[Any, Any]]:
    if isinstance(value, Mapping):
        return list(value.items())
    if isinstance(value, str):
        if len(value) > MAX_TAGS_HEADER_LENGTH:
            return []
        value = value.split(",")
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str) and "=" in item:
                k, _, v = item.partition("=")
                out.append((k, v))
        return out
    return []


def parse_tags(value: Any) -> dict[str, str] | None:
    """Valid cost tags as {key: value}, at most MAX_TAGS (the first ones
    sent; a repeated key keeps its first value); None when there are none."""
    tags: dict[str, str] = {}
    for raw_key, raw_value in _tag_pairs(value):
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            continue
        key = raw_key.strip().lower()
        val = raw_value.strip()
        if (
            not TAG_KEY_RE.fullmatch(key)
            or key.startswith(RESERVED_TAG_PREFIX)
            or not TAG_VALUE_RE.fullmatch(val)
            or key in tags
        ):
            continue
        tags[key] = val
        if len(tags) == MAX_TAGS:
            break
    return tags or None


def parse_ref(value: Any) -> str | None:
    """A team or project reference: a UUID (canonical form) or a name."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        return str(UUID(value))
    except ValueError:
        pass
    return value if NAME_RE.fullmatch(value) else None


def _as_uuid(ref: str) -> UUID | None:
    try:
        return UUID(ref)
    except ValueError:
        return None


@dataclass(frozen=True)
class Attribution:
    team_id: UUID | None = None
    project_id: UUID | None = None


async def _lookup(db: AsyncSession, model, tenant_id: UUID, refs: set[str]) -> dict[str, Any]:
    """{ref: row} for the refs naming a row of the tenant (by id or name)."""
    if not refs:
        return {}
    ids = {u for u in (_as_uuid(r) for r in refs) if u is not None}
    names = {r for r in refs if _as_uuid(r) is None}
    cond = []
    if ids:
        cond.append(model.id.in_(ids))
    if names:
        cond.append(model.name.in_(names))
    rows = (
        (await db.execute(select(model).where(model.tenant_id == tenant_id, or_(*cond))))
        .scalars()
        .all()
    )
    out: dict[str, Any] = {}
    for row in rows:
        out[str(row.id)] = row
        out[row.name] = row
    return {r: out[r] for r in refs if r in out}


async def resolve_refs(
    db: AsyncSession, tenant_id: UUID, pairs: Iterable[tuple[str | None, str | None]]
) -> dict[tuple[str | None, str | None], Attribution]:
    """Resolve (team ref, project ref) pairs against the tenant's teams and
    projects, with one query per table for the whole batch. Checked on
    ``tenant_id`` explicitly, not only by row-level security (opt-in)."""
    pairs = set(pairs)
    teams = await _lookup(db, Team, tenant_id, {t for t, _ in pairs if t})
    projects = await _lookup(db, Project, tenant_id, {p for _, p in pairs if p})
    out: dict[tuple[str | None, str | None], Attribution] = {}
    for team_ref, project_ref in pairs:
        project = projects.get(project_ref) if project_ref else None
        if project is not None:
            out[(team_ref, project_ref)] = Attribution(project.team_id, project.id)
            continue
        team = teams.get(team_ref) if team_ref else None
        out[(team_ref, project_ref)] = Attribution(team.id if team else None, None)
    return out


# ──────────────────────────────────────────────────────────────────────
# Tenant settings
# ──────────────────────────────────────────────────────────────────────


def settings_errors(value: Any) -> list[str]:
    """Problems with a ``settings.cost_tag_keys`` value (None clears it)."""
    if value is None:
        return []
    if not isinstance(value, list):
        return [f"settings.{SETTINGS_KEY} must be a list of tag keys"]
    if len(value) > MAX_ROLLUP_TAG_KEYS:
        return [f"settings.{SETTINGS_KEY} may list at most {MAX_ROLLUP_TAG_KEYS} keys"]
    errors = []
    for key in value:
        if (
            not isinstance(key, str)
            or not TAG_KEY_RE.fullmatch(key)
            or key.startswith(RESERVED_TAG_PREFIX)
        ):
            errors.append(
                f"settings.{SETTINGS_KEY}: {key!r} is not a tag key "
                "(lower-case letters, digits and _.:/-, not starting with ps:)"
            )
    if len(set(map(str, value))) != len(value):
        errors.append(f"settings.{SETTINGS_KEY} lists a key twice")
    return errors


def rollup_tag_keys(tenant_settings: Mapping[str, Any] | None) -> list[str]:
    """The tag keys the tenant's rollups keep (valid ones only)."""
    value = (tenant_settings or {}).get(SETTINGS_KEY)
    if not isinstance(value, list) or settings_errors(value):
        return []
    return list(value)


# ──────────────────────────────────────────────────────────────────────
# Group by
# ──────────────────────────────────────────────────────────────────────


class GroupByError(ValueError):
    pass


def parse_group_by(raw: str, allowed: Sequence[str], tenant_settings: Mapping | None) -> tuple[str, str | None]:
    """(dimension, tag key) for a ``group_by`` value: one of ``allowed`` or
    ``tag:<key>`` with the key in the tenant's settings.cost_tag_keys."""
    if raw.startswith(TAG_GROUP_PREFIX):
        key = raw[len(TAG_GROUP_PREFIX) :]
        if not TAG_KEY_RE.fullmatch(key):
            raise GroupByError(f"group_by: {key!r} is not a tag key")
        if key not in rollup_tag_keys(tenant_settings):
            raise GroupByError(
                f"group_by=tag:{key} needs '{key}' in the tenant's settings.{SETTINGS_KEY} "
                "(only those tag keys are kept in the usage rollups)"
            )
        return "tag", key
    if raw not in allowed:
        raise GroupByError(
            "group_by must be one of " + ", ".join(allowed) + " or tag:<key>"
        )
    return raw, None


# ──────────────────────────────────────────────────────────────────────
# Showback
# ──────────────────────────────────────────────────────────────────────

SHOWBACK_GROUP_BY = ("team", "project", "user", "service")
PERIOD_RE = re.compile(r"(\d{4})-(0[1-9]|1[0-2])")


def month_window(period: str) -> tuple[datetime, datetime]:
    """[start, end) of a ``YYYY-MM`` UTC month; ValueError otherwise."""
    m = PERIOD_RE.fullmatch(period)
    if not m:
        raise ValueError("period must be a month, YYYY-MM")
    year, month = int(m.group(1)), int(m.group(2))
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)
    return start, end


def allocate_cents(amounts: Sequence[Decimal]) -> list[int]:
    """Whole cents per amount (non-negative) that add up to the total's
    cents, rounded half up: each amount gets its floor, and the cents left
    go to the largest remainders (ties to the earlier amount)."""
    total = int((sum(amounts, Decimal(0)) / CENT).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    floors = [int((a / CENT).to_integral_value(rounding=ROUND_DOWN)) for a in amounts]
    left = total - sum(floors)
    order = sorted(
        range(len(amounts)),
        key=lambda i: (-(amounts[i] / CENT - floors[i]), i),
    )
    for i in order[: max(left, 0)]:
        floors[i] += 1
    return floors


def _showback_key(dimension: str, tag_key: str | None):
    t = UsageRollupDaily.__table__
    if dimension == "team":
        return t.c.team_id.cast(Text)
    if dimension == "project":
        return t.c.project_id.cast(Text)
    if dimension == "user":
        return t.c.user_id_hash
    if dimension == "service":
        return t.c.calling_service
    return t.c.tags[tag_key].astext


async def showback(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    dimension: str,
    tag_key: str | None,
    start: datetime,
    end: datetime,
) -> dict:
    """Cost and usage of [start, end) per group, from the daily rollups."""
    t = UsageRollupDaily.__table__
    key = _showback_key(dimension, tag_key).label("key")
    stmt = (
        select(
            key,
            func.sum(t.c.cost).label("cost"),
            func.sum(t.c.requests).label("requests"),
            func.sum(t.c.cost_requests).label("cost_requests"),
            func.sum(t.c.tokens_in).label("tokens_in"),
            func.sum(t.c.tokens_out).label("tokens_out"),
        )
        .where(t.c.tenant_id == tenant_id, t.c.bucket_start >= start, t.c.bucket_start < end)
        .group_by(key)
        .order_by(func.sum(t.c.cost).desc(), key.asc().nulls_first())
    )
    rows = (await db.execute(stmt)).mappings().all()
    groups = [r for r in rows if r["key"] is not None]
    unallocated = [r for r in rows if r["key"] is None]

    names: dict[str, dict] = {}
    if dimension in ("team", "project") and groups:
        names = await _names(db, tenant_id, dimension, [r["key"] for r in groups])

    def money(r) -> Decimal:
        return Decimal(r["cost"] or 0)

    parts = [*groups, *unallocated]
    cents = allocate_cents([money(r) for r in parts])

    def entry(r, c: int) -> dict:
        return {
            "cost_usd": format(money(r).quantize(Decimal("0.000001")), "f"),
            "cost_cents": c,
            "requests": int(r["requests"] or 0),
            "cost_requests": int(r["cost_requests"] or 0),
            "tokens_in": int(r["tokens_in"] or 0),
            "tokens_out": int(r["tokens_out"] or 0),
        }

    data = []
    for r, c in zip(groups, cents[: len(groups)], strict=True):
        extra = names.get(r["key"], {})
        data.append(
            {
                "key": r["key"],
                "name": extra.get("name"),
                "team_id": extra.get("team_id"),
                "team_name": extra.get("team_name"),
                **entry(r, c),
            }
        )
    empty = {"cost": 0, "requests": 0, "cost_requests": 0, "tokens_in": 0, "tokens_out": 0}
    un = entry(unallocated[0], cents[-1]) if unallocated else entry(empty, 0)
    total_cost = sum((money(r) for r in parts), Decimal(0))
    total = {
        "cost_usd": format(total_cost.quantize(Decimal("0.000001")), "f"),
        "cost_cents": sum(cents),
        "requests": sum(int(r["requests"] or 0) for r in parts),
        "cost_requests": sum(int(r["cost_requests"] or 0) for r in parts),
        "tokens_in": sum(int(r["tokens_in"] or 0) for r in parts),
        "tokens_out": sum(int(r["tokens_out"] or 0) for r in parts),
    }
    return {"data": data, "unallocated": un, "total": total}


async def _names(db: AsyncSession, tenant_id: UUID, dimension: str, keys: list[str]) -> dict[str, dict]:
    ids = [u for u in (_as_uuid(k) for k in keys) if u is not None]
    if dimension == "team":
        rows = (
            await db.execute(select(Team.id, Team.name).where(Team.tenant_id == tenant_id, Team.id.in_(ids)))
        ).all()
        return {str(r.id): {"name": r.name} for r in rows}
    rows = (
        await db.execute(
            select(Project.id, Project.name, Project.team_id, Team.name.label("team_name"))
            .join(Team, Team.id == Project.team_id)
            .where(Project.tenant_id == tenant_id, Project.id.in_(ids))
        )
    ).all()
    return {
        str(r.id): {"name": r.name, "team_id": str(r.team_id), "team_name": r.team_name}
        for r in rows
    }


async def names_for(db: AsyncSession, tenant_id: UUID, dimension: str, keys: Iterable[str | None]) -> dict[str, str]:
    """{id: name} for team or project ids (the usage series' ``name``)."""
    keys = [k for k in set(keys) if k]
    if not keys:
        return {}
    return {k: v["name"] for k, v in (await _names(db, tenant_id, dimension, keys)).items()}
