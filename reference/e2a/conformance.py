"""E2A conformance kit (v1.2, design notes 01 and 05): the Governed Entry Point rules.

Governed Entry Point principle: callers reach a class only through its public methods;
each public method is a distinct capability that delegates to protected hooks (the
subclass implements them) and private methods (the framework owns them).

    R1  error    A subclass does not override a framework-owned public method or a
                 framework-private (__) method.
    R2  warning  Code outside a class does not call that class's protected hooks
                 (an error with --strict; an error by default from v1.3).
    R3  info     An extra public method on a subclass: allowed if it is a capability
                 (its own caller or trigger, its own guarantees, not a shortcut to a hook).
    D1  warning  A subclass overrides a public method that became a protected hook in
                 v1.2 (get_endpoint -> _get_endpoint); removed in v1.3 (an error with --strict).

The rule sets are read from the e2a classes themselves, so they follow the framework:
framework-owned = the public, non-abstract methods of an e2a class; hooks = its
single-underscore methods.

    python -m e2a.conformance src/ [--strict] [--quiet]     exit 1 on an error

The pytest kit (AgentContract) checks the behaviour every agent must keep.
"""
import ast
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

SEVERITY = {'R1': 'error', 'R2': 'warning', 'R3': 'info', 'D1': 'warning'}
_FRAMEWORK_MODULES = ('e2a', 'e2a_base')
_DEPRECATED_HOOKS = {'get_endpoint': '_get_endpoint'}      # public in v1.1, protected from v1.2


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    path: str
    line: int
    where: str
    message: str

    def __str__(self) -> str:
        return f'{self.path}:{self.line}: {self.rule} {self.severity}: {self.where}: {self.message}'


@dataclass(frozen=True)
class _FrameworkClass:
    owned: frozenset          # framework-owned public methods (overriding them breaks R1)
    abstract_public: frozenset
    private: frozenset        # '__name' methods
    hooks: frozenset          # '_name' methods


def _framework_classes() -> Dict[str, _FrameworkClass]:
    import e2a
    found = {}
    for name in dir(e2a):
        cls = getattr(e2a, name)
        if not inspect.isclass(cls) or not cls.__module__.startswith('e2a.'):
            continue
        owned, abstract, private, hooks = set(), set(), set(), set()
        for klass in cls.__mro__:
            if not klass.__module__.startswith('e2a.'):
                continue
            prefix = f'_{klass.__name__}__'
            for attr, value in vars(klass).items():
                if not callable(value) and not isinstance(value, (staticmethod, classmethod, property)):
                    continue
                if attr.startswith(prefix):
                    private.add('__' + attr[len(prefix):])
                elif attr.startswith('__') and attr.endswith('__'):
                    continue
                elif attr.startswith('_'):
                    hooks.add(attr)
                elif getattr(getattr(cls, attr, None), '__isabstractmethod__', False):
                    abstract.add(attr)
                else:
                    owned.add(attr)
        found[name] = _FrameworkClass(frozenset(owned - abstract), frozenset(abstract),
                                      frozenset(private), frozenset(hooks))
    return found


def _framework_aliases(tree: ast.AST, known: Iterable[str]) -> Dict[str, str]:
    """local name -> e2a class name, from `from e2a(_base) import X [as Y]` (and e2a.X)."""
    aliases = {name: name for name in known}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split('.')[0] in _FRAMEWORK_MODULES:
            for item in node.names:
                if item.name in known:
                    aliases[item.asname or item.name] = item.name
    return aliases


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ''


def check_paths(paths: Iterable[str]) -> List[Finding]:
    """Check every .py file under the given files or folders."""
    files: List[Path] = []
    for p in map(Path, paths):
        files.extend(sorted(p.rglob('*.py')) if p.is_dir() else [p])
    sources = {str(f): f.read_text(encoding='utf-8') for f in files}
    return check_sources(sources)


def check_sources(sources: Dict[str, str]) -> List[Finding]:
    """Check source texts ({path: text}). Classes derived from e2a classes are followed
    across the given files (class B(A) where A(BaseAgent) is in another file)."""
    framework = _framework_classes()
    trees = {path: ast.parse(text, filename=path) for path, text in sources.items()}

    # pass 1: every class and the e2a classes it derives from (transitively)
    local_bases: Dict[str, List[str]] = {}
    for path, tree in trees.items():
        aliases = _framework_aliases(tree, framework)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                local_bases[node.name] = [aliases.get(_base_name(b), _base_name(b)) for b in node.bases]

    def ancestors(name: str, seen: Optional[Set[str]] = None) -> Set[str]:
        seen = seen if seen is not None else set()
        result = set()
        for base in local_bases.get(name, []):
            if base in framework:
                result.add(base)
            elif base in local_bases and base not in seen:
                seen.add(base)
                result |= ancestors(base, seen)
        return result

    all_hooks = frozenset().union(*(fc.hooks for fc in framework.values()))
    findings: List[Finding] = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            roots = ancestors(node.name)
            if not roots:
                continue
            owned = frozenset().union(*(framework[r].owned for r in roots))
            known = owned | frozenset().union(*(framework[r].abstract_public | framework[r].hooks for r in roots))
            private = frozenset().union(*(framework[r].private for r in roots))
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                name, where = item.name, f'{node.name}.{item.name}'
                if name in _DEPRECATED_HOOKS and name in owned:
                    findings.append(Finding('D1', SEVERITY['D1'], path, item.lineno, where,
                                            f'{name}() is a deprecated public alias; implement '
                                            f'{_DEPRECATED_HOOKS[name]}() (removed in v1.3)'))
                elif name in owned:
                    findings.append(Finding('R1', SEVERITY['R1'], path, item.lineno, where,
                                            f'overrides the framework-owned public method {name}() of '
                                            f'{"/".join(sorted(roots))}; implement its protected hooks instead'))
                elif name in private:
                    findings.append(Finding('R1', SEVERITY['R1'], path, item.lineno, where,
                                            f'redefines the framework-private {name}(); it is never called '
                                            f'by the framework (name mangling) and must not be replaced'))
                elif not name.startswith('_') and name not in known:
                    findings.append(Finding('R3', SEVERITY['R3'], path, item.lineno, where,
                                            'extra public method: keep it only if it is a capability '
                                            '(own caller or trigger, own guarantees, not a shortcut to a hook)'))
        findings.extend(_outside_hook_calls(path, tree, all_hooks))
    return sorted(findings, key=lambda f: (f.path, f.line, f.rule))


def _outside_hook_calls(path: str, tree: ast.AST, hooks: frozenset) -> List[Finding]:
    """R2: obj._hook(...) where obj is not self, cls or super()."""
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        name, target = node.func.attr, node.func.value
        if name not in hooks or (name.startswith('__') and name.endswith('__')):
            continue
        if isinstance(target, ast.Name) and target.id in ('self', 'cls'):
            continue
        if isinstance(target, ast.Call) and isinstance(target.func, ast.Name) and target.func.id == 'super':
            continue
        try:
            text = ast.unparse(node.func)
        except Exception:                       # pragma: no cover
            text = name
        found.append(Finding('R2', SEVERITY['R2'], path, node.lineno, text,
                             f'calls the protected hook {name}() from outside its class; '
                             f'use the class\'s public method'))
    return found


def failed(findings: Iterable[Finding], strict: bool = False) -> bool:
    """R1 always fails; R2 and D1 fail with strict (the default from v1.3); R3 never fails."""
    return any(f.rule == 'R1' or (strict and f.rule in ('R2', 'D1')) for f in findings)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog='python -m e2a.conformance',
                                     description='Check the E2A Governed Entry Point rules (R1-R3).')
    parser.add_argument('paths', nargs='+', help='files or folders to check')
    parser.add_argument('--strict', action='store_true', help='R2 and D1 findings fail too (the v1.3 default)')
    parser.add_argument('--quiet', action='store_true', help='do not print R3 (info) findings')
    args = parser.parse_args(argv)
    findings = check_paths(args.paths)
    for f in findings:
        if not (args.quiet and f.rule == 'R3'):
            print(f)
    counts = {r: sum(f.rule == r for f in findings) for r in ('R1', 'R2', 'D1', 'R3')}
    bad = failed(findings, args.strict)
    print(f"e2a.conformance: {counts['R1']} R1 error(s), {counts['R2']} R2 warning(s), "
          f"{counts['D1']} D1 deprecation(s), {counts['R3']} R3 note(s): {'FAILED' if bad else 'OK'}")
    return 1 if bad else 0


# ======================================================================
# Contract-test kit (pytest)
# ======================================================================

class AgentContract:
    """Behaviour every E2A agent keeps. Subclass it in a test module:

        class TestRouterAgent(AgentContract):
            def make_agent(self): return RouterAgent()
            valid_state = {'tenant_id': 't1', 'query': 'where is my order?'}
            config = {...}                      # optional; must include an llm_service for a model step

    Inherited tests: run() never raises on a hook error; a model step below
    min_confidence falls back and commits nothing; nothing material happens after the
    deadline; the tenant of the request reaches the result."""

    valid_state: Dict = {}
    config: Optional[Dict] = None

    def make_agent(self):
        raise NotImplementedError('AgentContract: implement make_agent() (or set `agent`)')

    # --------------------------------------------------------------- helpers
    def _agent(self):
        agent = getattr(self, 'agent', None)
        return agent if agent is not None else self.make_agent()

    def _config(self, **extra) -> Dict:
        config = dict(self.config or {})
        if 'llm_service' not in config:
            from e2a_simulated_llm import SimulatedLLMService, always
            config['llm_service'] = SimulatedLLMService().add_rule('contract', always(), '{}')
        config.update(extra)
        return config

    def _state(self) -> Dict:
        import copy
        state = copy.deepcopy(self.valid_state)
        state.setdefault('tenant_id', 'contract-tenant')
        return state

    @staticmethod
    def _spy_material(monkeypatch) -> List[str]:
        from e2a.context import ModifyAccess
        calls: List[str] = []

        def spy(self, name, payload, **kw):
            from e2a.core import PhysicalOutcome
            calls.append(name)
            return {'status': 'SPY', 'physical_success': False, 'outcome': PhysicalOutcome.NOT_COMMITTED}

        monkeypatch.setattr(ModifyAccess, '_call', spy)
        return calls

    # ----------------------------------------------------------------- tests
    def test_run_never_raises_on_a_hook_error(self):
        from e2a._contract import declares_ctx
        agent = self._agent()

        def broken(*args, **kwargs):
            raise RuntimeError('contract: injected hook failure')

        if declares_ctx(agent._build_messages):
            def broken_v2(state, ctx):          # noqa: ARG001 - the v1.2 signature
                raise RuntimeError('contract: injected hook failure')
            agent._build_messages = broken_v2
        else:
            agent._build_messages = broken
        state = agent.run(self._state(), self._config())
        assert isinstance(state, dict)

    def test_a_model_step_below_min_confidence_falls_back_and_commits_nothing(self, monkeypatch):
        calls = self._spy_material(monkeypatch)
        state = self._agent().run(self._state(), self._config(min_confidence=1.01), idempotency_key='contract-1')
        if state.get('evaluation') is None and 'response' not in state:
            return                                  # a deterministic agent: no model step to evaluate
        if state.get('evaluation') is not None:
            assert state['evaluation'] == 'FALLBACK'
            assert calls == [], f'material tools called after a rejected evaluation: {calls}'

    def test_nothing_material_happens_after_the_deadline(self, monkeypatch):
        from e2a.core import Deadline
        calls = self._spy_material(monkeypatch)
        self._agent().run(self._state(), self._config(), idempotency_key='contract-2', deadline=Deadline.after(0))
        assert calls == [], f'material tools called after the deadline: {calls}'

    def test_the_request_tenant_reaches_the_result(self):
        state = self._agent().run(self._state(), self._config())
        assert state.get('tenant_id') == self._state()['tenant_id']


if __name__ == '__main__':                          # pragma: no cover
    sys.exit(main())
