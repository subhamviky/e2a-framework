"""Capabilities: what a bound service can do on its platform (v1.6.0, design note MP-02, P4).

The same model can expose different features on different platforms (for example, a
provider's MCP connector exists on one platform and not on another). A deployment lists the
features it relies on; Bindings.verify() refuses to start when no bound object provides one,
instead of failing on the first request.

A bound object declares a `capabilities` set (class attribute), or implements the protected
hook `_capabilities_for(config)` when the set depends on configuration (platform, region).
Names are strings from a registry: E2A registers the common ones below, and a provider
package may add its own with register_capability(), so a new feature needs no E2A release
while a typo still fails loudly.
"""
from typing import Any, Dict, FrozenSet, Iterable, List

__all__ = ['CAPABILITIES', 'CapabilityError', 'register_capability', 'capabilities_of', 'missing']

CAPABILITIES = {
    # model calls
    'tool_use', 'structured_output', 'streaming', 'prompt_caching', 'reasoning_effort',
    'refusal_fallback', 'refusal_fallback_server', 'mcp_connector', 'server_web_search', 'batch',
    # retrieval
    'embeddings', 'vector_search', 'hybrid_search', 'managed_retrieval', 'rerank',
    # safety, prompts, runtimes
    'guardrails', 'prompt_registry', 'mcp_gateway', 'managed_agent_runtime',
    # substrate
    'idempotency_store', 'checkpoint_store', 'transactional_store', 'lease', 'telemetry',
    'infra_as_code', 'release_pipeline',
}


class CapabilityError(RuntimeError):
    """A deployment requires a feature that none of its bound objects provides."""


def register_capability(name: str) -> str:
    """Add a capability name (a provider package's own feature). Idempotent."""
    if not name or not name.replace('_', '').isalnum() or name != name.lower():
        raise ValueError(f'capability names are lower_snake_case: {name!r}')
    CAPABILITIES.add(name)
    return name


def _checked(names: Iterable[Any]) -> FrozenSet[str]:
    out = set()
    for name in names:
        value = getattr(name, 'value', name)          # accept str enums
        if value not in CAPABILITIES:
            raise ValueError(f'unknown capability {value!r}: register it with e2a.capabilities.register_capability')
        out.add(value)
    return frozenset(out)


def capabilities_of(obj: Any, config: Dict[str, Any] = None) -> FrozenSet[str]:
    """What a bound object declares: `_capabilities_for(config)`, else `capabilities`.
    (`capabilities_for`, the public name some early providers used, is read too.)"""
    if obj is None:
        return frozenset()
    for hook in ('_capabilities_for', 'capabilities_for'):
        fn = getattr(obj, hook, None)
        if callable(fn):
            return _checked(fn(config or {}))
    return _checked(getattr(obj, 'capabilities', ()) or ())


def missing(required: Iterable[Any], provided: Iterable[Any]) -> List[str]:
    """The required capabilities none of the provided sets has, sorted."""
    return sorted(_checked(required) - _checked(provided))
