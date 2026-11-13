"""Internal: calling hooks under the BOPF-style contract (v1.2).

A hook declares a parameter named `ctx`. It receives exactly the parameters it declares,
from those its step makes available; declaring one the step does not offer (for example
io_modify before the act step) is an error. Since v1.3 a hook without `ctx` (the v1.1
keyword signature) is refused when its class is defined.
"""
import inspect
from typing import Any, Callable, Dict

def declares_ctx(fn: Callable) -> bool:
    try:
        return 'ctx' in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def call_hook(fn: Callable, step: str, available: Dict[str, Any]) -> Any:
    """Call a v1.2 hook with the declared parameters, taken by name from `available`."""
    params = inspect.signature(fn).parameters
    args = {}
    for name, p in params.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if name in available:
            args[name] = available[name]
        elif p.default is p.empty:
            offered = ', '.join(sorted(available))
            raise TypeError(f"{getattr(fn, '__qualname__', fn)}: '{name}' is not available in {step} "
                            f"(this step offers: {offered})")
    return fn(**args)


def refuse_v11_hooks(cls: type, hooks) -> None:
    """v1.3: a hook a class defines must declare the v1.2 parameters ('state, ctx, ...').
    The v1.1 keyword signature (config=None, **kwargs) was deprecated in v1.2 and is refused
    here, when the class is defined, naming the hook."""
    for name in hooks:
        fn = cls.__dict__.get(name)
        if callable(fn) and not declares_ctx(fn):
            raise TypeError(f"{cls.__qualname__}.{name} uses the v1.1 keyword signature, removed in E2A v1.3: "
                            f"declare the v1.2 contract parameters ('state, ctx, ...'); see the Implementation "
                            f"Playbook 2.20")
