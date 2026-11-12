"""Internal: calling hooks under the BOPF-style contract (v1.2).

A hook written for v1.2 declares a parameter named `ctx`. It receives exactly the
parameters it declares, from those its step makes available; declaring one the step
does not offer (for example io_modify before the act step) is an error. A hook
without `ctx` is a v1.1 hook and is called the v1.1 way, once per class with a
DeprecationWarning.
"""
import inspect
import warnings
from typing import Any, Callable, Dict

_WARNED = set()


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


def warn_legacy(owner: type, hook: str) -> None:
    """One warning per class (naming the first hook seen), not one per hook."""
    if owner not in _WARNED:
        _WARNED.add(owner)
        warnings.warn(f"{owner.__qualname__} uses the v1.1 keyword signature (for example {hook}); declare "
                      f"the v1.2 contract parameters ('state, ctx, ...'). The v1.1 form is removed in v1.3.",
                      DeprecationWarning, stacklevel=3)
