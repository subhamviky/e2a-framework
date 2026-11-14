"""Bindings and profiles: the composition root (v1.6.0, design note MP-02, proposal P1).

A profile names the class bound to each E2A seam and supplies configuration values:

    {"name": "anthropic_bedrock", "provider": "anthropic", "cloud": "aws",
     "features": ["tool_use", "guardrails"],
     "extends": "aws_base",                                   (optional: a profile to overlay)
     "bindings": {"llm_service": "e2a_providers.anthropic.bedrock:BedrockClaudeLLMService"},
     "config": {"model_id": "${LLM_MODEL_ID:-anthropic.claude-opus-5-5}", "llm_max_latency": 60}}

    from e2a.bindings import bindings_from_profile
    b = bindings_from_profile()                 # E2A_PROVIDER_PROFILE, or a name / path
    service = MyService(llm=b.llm_service)      # seams are built lazily, on first access
    workflow.execute(state, config=b.config(tools=tools))
    b.verify()                                  # at startup: required capabilities present

Rules
* **Only registered packages may be bound.** A profile is configuration, so it must not be
  able to import arbitrary code. Bindable: E2A's own modules, and packages registered as an
  entry point in the group ``e2a.providers`` (``e2a_providers = "e2a_providers"`` in the
  package's pyproject) or with register_provider_package(). A class passed in code
  (``factories=``) is not restricted: code is already trusted.
* **The profile is a configuration layer below the environment:** its values reach the
  config dict as ``profile_settings``, so resolve_config() reads
  ``kwargs > config > env > profile > default``. Explicit overrides are the config layer.
* **Formats:** JSON always; YAML when PyYAML is installed (``e2a-framework[profiles]``).
  ``${VAR}`` / ``${VAR:-default}`` placeholders are resolved at load; an unset one without a
  default is dropped, so the key falls through to E2A's own env and default.
* **Unknown seams and unknown profile fields are errors.**
"""
import importlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Union

from .capabilities import CapabilityError, capabilities_of, missing
from .config import E2A_CONFIG, resolve_config

__all__ = ['SEAMS', 'Profile', 'Bindings', 'bindings_from_profile', 'register_provider_package',
           'available_profiles']

SEAMS = (
    'llm_service',            # BaseLLMAgentService            -> config['llm_service']
    'embedding_model',        # BaseEmbeddingModel
    'vector_index',           # BaseVectorIndex
    'reranker',               # a reranker for BaseRAGPipeline._rerank
    'rag_pipeline',           # BaseRAGPipeline
    'guardrail_stage',        # IngressStage for a governance engine
    'prompt_registry',        # BasePromptRegistry
    'mcp_gateway_tool',       # MCPToolService through a managed gateway
    'delegated_runtime',      # a managed agent runtime (a BaseLLMAgentService)
    'observability_engine',   # BaseObservability              -> config['observability_engine']
    'idempotency_store',      # BaseIdempotencyStore           -> config['idempotency_store']
    'checkpoint_store',       # BaseCheckpointStore            -> config['checkpoint_store']
    'credential_provider',    # BaseCredentialProvider         -> config['credential_provider']
    'infra_provisioner',      # BaseInfraProvisioner (CI/CD time)
    'release_pipeline',       # BasePipeline (CI/CD time)
)
_CONFIG_SEAMS = ('llm_service', 'observability_engine', 'idempotency_store', 'checkpoint_store',
                 'credential_provider')
_PROFILE_FIELDS = {'name', 'provider', 'cloud', 'description', 'features', 'bindings', 'config', 'extends'}
_BUILTIN_PACKAGES = {'e2a', 'e2a_simulated_llm', 'e2a_inmemory_adapters', 'e2a_adapters'}
_REGISTERED: set = set()
_PLACEHOLDER = re.compile(r'\$\{([A-Z0-9_]+)(?::-(.*?))?\}')
_DROP = object()
Factory = Union[str, Callable[..., Any]]


# ------------------------------------------------------------------ provider packages
def register_provider_package(name: str) -> None:
    """Allow a package's classes to be bound from profiles, and search its profiles."""
    importlib.import_module(name)
    _REGISTERED.add(name)


def _provider_packages() -> List[str]:
    names = set(_BUILTIN_PACKAGES) | _REGISTERED
    try:
        from importlib.metadata import entry_points
        names |= {ep.value.split(':')[0] for ep in entry_points(group='e2a.providers')}
    except Exception:                                    # pragma: no cover - metadata unavailable
        pass
    return sorted(names)


def _bindable(module: str) -> bool:
    return any(module == p or module.startswith(p + '.') for p in _provider_packages())


def _import(target: str) -> Callable[..., Any]:
    module, _, attr = target.partition(':')
    if not attr:
        raise ValueError(f"binding '{target}' must be 'package.module:ClassName'")
    if not _bindable(module):
        raise PermissionError(f"binding '{target}': '{module}' is not in a registered provider package "
                              f"(register it with the 'e2a.providers' entry point or register_provider_package)")
    return getattr(importlib.import_module(module), attr)


def _profile_dirs() -> List[Path]:
    dirs = [Path(__file__).parent / 'profiles']
    for name in _provider_packages():
        if name in _BUILTIN_PACKAGES:
            continue
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        dirs.append(Path(module.__file__).parent)
    return dirs


# ------------------------------------------------------------------ profiles
def _interpolate(value: Any) -> Any:
    if isinstance(value, dict):
        out = {k: _interpolate(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v is not _DROP}
    if isinstance(value, list):
        return [v for v in (_interpolate(v) for v in value) if v is not _DROP]
    if not isinstance(value, str):
        return value
    whole = _PLACEHOLDER.fullmatch(value.strip())
    if whole:
        resolved = os.environ.get(whole.group(1), whole.group(2))
        return _DROP if resolved is None else resolved
    return _PLACEHOLDER.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ''), value)


def _typed(config: Dict[str, Any]) -> Dict[str, Any]:
    """A placeholder yields a string; cast it with the key's registered type."""
    out = {}
    for key, value in config.items():
        spec = E2A_CONFIG.get(key)
        out[key] = spec.cast(value) if isinstance(value, str) and spec and spec.cast not in (str, None) else value
    return out


def _merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def _read(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding='utf-8')
    if path.suffix == '.json':
        return json.loads(text)
    try:
        import yaml
    except ImportError as e:
        raise ImportError(f"{path.name}: YAML profiles need PyYAML: pip install 'e2a-framework[profiles]'") from e
    return yaml.safe_load(text)


def _find(name: str) -> Path:
    for directory in _profile_dirs():
        for suffix in ('.json', '.yaml', '.yml'):
            hits = sorted(directory.glob(f'**/profiles/{name}{suffix}')) + sorted(directory.glob(f'{name}{suffix}'))
            if hits:
                return hits[0]
    raise FileNotFoundError(f"no profile named '{name}' (available: {', '.join(available_profiles()) or 'none'})")


def available_profiles() -> List[str]:
    names = set()
    for directory in _profile_dirs():
        for suffix in ('json', 'yaml', 'yml'):
            names |= {p.stem for p in directory.glob(f'**/profiles/*.{suffix}')}
            names |= {p.stem for p in directory.glob(f'*.{suffix}') if directory.name == 'profiles'}
    return sorted(names)


@dataclass(frozen=True)
class Profile:
    name: str
    provider: str
    cloud: str = ''
    description: str = ''
    features: List[str] = field(default_factory=list)
    bindings: Dict[str, str] = field(default_factory=dict)
    config: Dict[str, Any] = field(default_factory=dict)
    source: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any], source: Optional[str] = None, _seen: tuple = ()) -> 'Profile':
        unknown = set(data) - _PROFILE_FIELDS
        if unknown:
            raise ValueError(f"profile {source or data.get('name')}: unknown fields {sorted(unknown)}")
        if data.get('extends'):
            parent_name = data['extends']
            if parent_name in _seen:
                raise ValueError(f'profile {data.get("name")}: circular extends ({" -> ".join(_seen + (parent_name,))})')
            parent = cls.load(parent_name, _seen=_seen + (data.get('name', '?'),))
            base = {'name': parent.name, 'provider': parent.provider, 'cloud': parent.cloud,
                    'description': parent.description, 'features': parent.features,
                    'bindings': parent.bindings, 'config': parent.config}
            data = _merge(base, {k: v for k, v in data.items() if k != 'extends'})
        unknown_seams = set(data.get('bindings') or {}) - set(SEAMS)
        if unknown_seams:
            raise ValueError(f"profile {data.get('name')}: unknown seams {sorted(unknown_seams)}")
        return cls(name=data['name'], provider=data['provider'], cloud=data.get('cloud', ''),
                   description=data.get('description', ''), features=list(data.get('features') or []),
                   bindings=dict(data.get('bindings') or {}),
                   config=_typed(_interpolate(dict(data.get('config') or {}))), source=source)

    @classmethod
    def load(cls, name_or_path: str, _seen: tuple = ()) -> 'Profile':
        path = Path(name_or_path)
        if not path.suffix:
            path = _find(name_or_path)
        return cls.from_dict(_read(path), source=str(path), _seen=_seen)


# ------------------------------------------------------------------ bindings
class Bindings:
    """Lazily built seam objects, plus the profile's configuration layer."""

    def __init__(self, profile: Profile, factories: Mapping[str, Factory] = None,
                 overrides: Optional[Mapping[str, Any]] = None) -> None:
        factories = {**profile.bindings, **dict(factories or {})}
        unknown = set(factories) - set(SEAMS)
        if unknown:
            raise ValueError(f'profile {profile.name}: unknown seams {sorted(unknown)}')
        self.profile, self._factories = profile, factories
        overrides = dict(overrides or {})
        self._built: Dict[str, Any] = {k: v for k, v in overrides.items() if k in SEAMS}
        self._overrides = {k: v for k, v in overrides.items() if k not in SEAMS}

    def _layers(self) -> Dict[str, Any]:
        """What a bound object is constructed with: the overrides as the config layer and the
        profile as the layer below the environment."""
        return {**self._overrides, 'profile_settings': dict(self.profile.config)}

    def get(self, seam: str) -> Any:
        if seam not in SEAMS:
            raise KeyError(seam)
        if seam not in self._built:
            factory = self._factories.get(seam)
            if factory is None:
                self._built[seam] = None
            else:
                target = _import(factory) if isinstance(factory, str) else factory
                build = getattr(target, 'from_config', None)
                self._built[seam] = build(self._layers()) if callable(build) else target(config=self._layers())
        return self._built[seam]

    def __getattr__(self, seam: str) -> Any:
        if seam in SEAMS:
            return self.get(seam)
        raise AttributeError(seam)

    def bound(self) -> List[str]:
        return [s for s in SEAMS if s in self._factories or self._built.get(s) is not None]

    def setting(self, key: str) -> Any:
        """A key as the bound objects see it (kwargs aside)."""
        return resolve_config(key, self._layers())

    def config(self, **extra: Any) -> Dict[str, Any]:
        """The config dict for BaseWorkflow.execute() / BaseAgent.run(): the overrides, the
        profile layer, and the bound engines E2A reads from config. `extra` is added last."""
        cfg = self._layers()
        for seam in _CONFIG_SEAMS:
            obj = self.get(seam) if seam in self.bound() else None
            if obj is not None:
                cfg[seam] = obj
        cfg.update(extra)
        return cfg

    def verify(self, required: Optional[Iterable[str]] = None) -> Dict[str, List[str]]:
        """Build every bound seam and check the required capabilities (the argument, else the
        profile's `features`, else `required_capabilities`). Raises CapabilityError."""
        if required is None:
            required = self.profile.features or self.setting('required_capabilities') or []
        provided: Dict[str, List[str]] = {}
        for seam in self.bound():
            caps = capabilities_of(self.get(seam), self._layers())
            if caps:
                provided[seam] = sorted(caps)
        gaps = missing(required, [c for caps in provided.values() for c in caps])
        if gaps:
            raise CapabilityError(f"profile '{self.profile.name}' requires {gaps}, which none of its bindings "
                                  f"provides ({self.profile.provider}/{self.profile.cloud or '-'})")
        return provided


def bindings_from_profile(profile: Union[None, str, Profile] = None, *,
                          overrides: Optional[Mapping[str, Any]] = None,
                          factories: Optional[Mapping[str, Factory]] = None,
                          verify: bool = False) -> Bindings:
    """profile: a Profile, a name or path, or None for E2A_PROVIDER_PROFILE.
    overrides: config keys (the config layer) and/or already-built seam objects.
    factories: classes or callables to bind in code (not restricted to registered packages)."""
    if not isinstance(profile, Profile):
        name = profile or resolve_config('provider_profile', dict(overrides or {}))
        if not name:
            raise ValueError('no profile: pass one or set E2A_PROVIDER_PROFILE')
        profile = Profile.load(name)
    bindings = Bindings(profile, factories, overrides)
    if verify:
        bindings.verify()
    return bindings
