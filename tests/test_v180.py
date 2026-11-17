"""v1.8.0 (hub design note SI-01): service callers granted tenants as roles, require_roles(),
and a BaseException inside a workflow step leaves the claim unsettled (E2A backlog, 17 Nov 2026)."""
import pytest

from e2a import ClaimMapIdentityResolver, IdentityRefused, require_roles
from e2a_inmemory_adapters import InMemoryIdempotencyStore

from test_workflow_steps import Flow, Payer, config, request

SERVICE = {"iss": "https://login.microsoftonline.com/dir/v2.0", "tid": "dir", "azp": "o2c-app",
           "roles": ["settlement.submit", "tenant:acme", "tenant:globex"]}
SERVICE_CFG = {"identity_claim_map": {"tenant_id": "tid", "subject": "azp", "roles": "roles", "issuer": "iss"},
               "identity_tenants_from_roles": "tenant:"}


def resolve(claims=SERVICE, tenant="acme", **cfg):
    return ClaimMapIdentityResolver({**SERVICE_CFG, **cfg}).resolve(claims, True, tenant)


def test_a_service_acts_for_a_tenant_it_is_granted():
    identity = resolve(tenant="globex")
    assert identity.tenant_id == "globex" and identity.subject == "o2c-app"
    assert "settlement.submit" in identity.roles


@pytest.mark.parametrize("tenant", ["initech", None, ""])
def test_a_service_is_refused_a_tenant_it_is_not_granted_or_none(tenant):
    with pytest.raises(IdentityRefused):
        resolve(tenant=tenant)


def test_the_tenant_claim_is_not_used_for_a_service_caller():
    # the directory id in tid would otherwise map to a tenant; for services only roles count
    with pytest.raises(IdentityRefused):
        resolve(tenant="dir", identity_tenant_map={"dir": "dir"})


def test_trusted_issuers_still_apply_to_service_callers():
    with pytest.raises(IdentityRefused):
        resolve(identity_trusted_issuers=["https://other"])


def test_without_the_prefix_users_resolve_as_before():
    user = {"iss": "x", "tenant_id": "acme", "sub": "u1", "roles": ["sales"]}
    identity = ClaimMapIdentityResolver({}).resolve(user, True, "acme")
    assert identity.tenant_id == "acme" and identity.roles == ["sales"]


def test_require_roles_checks_the_action():
    identity = resolve()
    assert require_roles(identity, "settlement.submit") is identity
    with pytest.raises(IdentityRefused, match="settlement.adjust"):
        require_roles(identity, "settlement.adjust")


class ProcessDies(BaseException):
    """Stands for task cancellation or SystemExit inside a step."""


class DyingPayer(Payer):
    def _act(self, state, ctx, io_modify):
        raise ProcessDies()


def test_a_base_exception_in_a_step_leaves_the_claim_in_progress():
    cfg, flow = config(), Flow()
    flow.payer = DyingPayer()
    with pytest.raises(ProcessDies):
        flow.execute(request(), cfg)
    store: InMemoryIdempotencyStore = cfg["idempotency_store"]
    assert store.state_of("pay-u1-10.00", "t1") == "IN_PROGRESS"      # not SUCCEEDED with partial state
    assert flow.execute(request(), cfg)["status"] == "IN_PROGRESS"    # a repeat waits; recovery takes over later


def test_an_ordinary_exception_still_fails_the_claim():
    class FailingPayer(Payer):
        def _act(self, state, ctx, io_modify):
            raise RuntimeError("boom")
    cfg, flow = config(), Flow()
    flow.payer = FailingPayer()
    flow.execute(request(), cfg)
    assert cfg["idempotency_store"].state_of("pay-u1-10.00", "t1") != "SUCCEEDED"
