"""Every collector route declares what may call it (#341).

Walks the mounted app, so a new route cannot ship without a scope:

  - operator routes (/api/v1/operator and the operator integration routes)
    declare an ``operator:*`` scope (collector.operator_auth);
  - tenant admin routes (/api/v1/admin) declare an ``admin:*`` scope;
  - every other tenant route authenticates an ingest key with
    ``resolve_tenant(request, <scope>, ...)``, naming at least one
    ``tenant_keys.KEY_SCOPES`` scope;
  - partner routes (/partner/v1, /oauth/token) use partner credentials and
    are covered by tests/test_partner_scopes.py;
  - SCIM routes (/scim/v2) declare a ``scim:*`` scope (collector.scim), or
    take any SCIM token (ServiceProviderConfig);
  - the SSO login and callback (/api/v1/sso) are public by design: the
    identity provider authenticates the user (collector.sso).

No database needed.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap

from collector import tenant_keys
from collector.admin_auth import ADMIN_SCOPES
from collector.app import app
from collector.operator_auth import OPERATOR_SCOPES
from collector.scim import SCIM_SCOPES
from fastapi.routing import APIRoute

# Unauthenticated by design.
PUBLIC = {
    ("GET", "/metrics"),
    ("GET", "/readyz"),
    ("GET", "/api/v1/sso/{tenant_id}/login"),
    ("GET", "/api/v1/sso/callback"),
}
# Partner credentials (OAuth client credentials / partner API key).
PARTNER_PREFIXES = ("/partner/v1/", "/oauth/")

_KEY_SCOPE_NAMES = {
    "INGEST_WRITE": tenant_keys.INGEST_WRITE,
    "REGISTRY_READ": tenant_keys.REGISTRY_READ,
    "REGISTRY_ATTEST": tenant_keys.REGISTRY_ATTEST,
}


def _routes(routes):
    """Every APIRoute, including those under included routers (newer
    FastAPI keeps them behind an ``_IncludedRouter`` wrapper)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _routes(route.original_router.routes)


def _declared(route: APIRoute, attr: str) -> set[str]:
    return {getattr(d.call, attr, None) for d in route.dependant.dependencies} - {None}


def _resolve_tenant_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None))
        == "resolve_tenant"
    ]


def _scope_of(arg: ast.expr) -> str | None:
    """The ingest key scope an argument names, or None."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Attribute):
        return _KEY_SCOPE_NAMES.get(arg.attr)
    if isinstance(arg, ast.Starred):
        return "*"  # admin_auth: *sorted(ALL_KEY_SCOPES), checked below
    return None


def test_routes_were_found():
    paths = {r.path for r in _routes(app.routes)}
    assert "/ingest/events" in paths
    assert any(p.startswith("/api/v1/operator/") for p in paths)
    assert any(p.startswith("/api/v1/admin/") for p in paths)


def test_every_route_declares_a_scope():
    unscoped = []
    for route in _routes(app.routes):
        for method in route.methods:
            key = (method, route.path)
            if key in PUBLIC or route.path.startswith(PARTNER_PREFIXES):
                continue
            operator = _declared(route, "operator_scope")
            admin = _declared(route, "admin_scope")
            scim = _declared(route, "scim_scope")
            assert len(operator) + len(admin) + len(scim) <= 1, key
            if route.path.startswith("/scim/v2/"):
                assert scim and scim <= set(SCIM_SCOPES) | {"any"}, key
                continue
            assert not scim, key
            if operator:
                assert operator <= set(OPERATOR_SCOPES), key
                continue
            if admin:
                assert admin <= set(ADMIN_SCOPES), key
                assert not route.path.startswith("/api/v1/operator/"), key
                continue
            assert not route.path.startswith(("/api/v1/operator/", "/api/v1/admin/")), (
                key
            )
            tree = ast.parse(textwrap.dedent(inspect.getsource(route.endpoint)))
            calls = _resolve_tenant_calls(tree)
            scopes = [_scope_of(a) for call in calls for a in call.args[1:]]
            if not calls or not scopes or None in scopes:
                unscoped.append(key)
                continue
            assert set(scopes) <= set(tenant_keys.KEY_SCOPES), key
    assert not unscoped, f"routes without a declared scope: {sorted(unscoped)}"


def test_every_resolve_tenant_call_names_a_scope():
    """Also catches helpers that call resolve_tenant outside a route body."""
    pkg = pathlib.Path(inspect.getfile(tenant_keys)).parent
    missing = []
    for path in sorted(pkg.glob("*.py")):
        for call in _resolve_tenant_calls(ast.parse(path.read_text())):
            scopes = [_scope_of(a) for a in call.args[1:]]
            if not scopes or None in scopes:
                missing.append(f"{path.name}:{call.lineno}")
    assert not missing, f"resolve_tenant called without a scope: {missing}"
