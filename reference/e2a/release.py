"""E2A: ProjectReleaseGate, a release gate every project can reuse (v1.2.2, design note C-04, D-1).

BasePipeline.run_pipeline() (inherited, public) runs seven fixed stages and stops at the first
failure. ProjectReleaseGate gives the generic ones a working default and turns the domain ones
into one hook per case:

    static_security       secret patterns in the project's files                 (scan_paths, secret_patterns)
    test_execution        python -m pytest -q in the project root               (skipped with run_tests=False)
    scaffold_compliance   capacity equation + e2a.conformance over src/, strict  (conformance_strict)
    ragas_gate            average of _score_case(case) over evals/golden.json    hook: _score_case
    artifact_compilation  compileall over src/                                   hook: _compile_artifact
    dynamic_security      _probe(case) over evals/redteam.json                   hook: _probe
    deployment_rollout    records that nothing was deployed                      hook: _execute_deployment_rollout

A missing evaluation or red-team set is reported as NOT_CONFIGURED, never as a silent pass.

v1.9.0 (hub design note EV-01):
  * a case may declare `expect` (exact, contains, forbidden, schema, refusal, no_pii, grounded,
    trajectory; see e2a.evals.score_case). Then the default _score_case / _probe run the service
    with _run_case(case) (one hook: run it on case['input'], return the state) and score the
    result. A case without `expect`, or a project without _run_case, still fails: fail-closed;
  * eval_trials > 1 runs every case that many times: mean, minimum, standard deviation and
    pass@k per case; the gate scores the mean;
  * the report says what kind of evidence it is: `wiring` (simulated or replayed model: the gate's
    mechanics and the service's wiring), `quality` (recorded answers of a real model, set by the
    project) or `live` (E2A_SWAP_LIVE=true: real calls), with the profile and model.

    python -m e2a.release [--root .] [--skip-tests] [--report release-gate-report.json]

A project gate is a subclass; its module calls run_release_gate(gate_class=MyGate).
"""
import argparse
import compileall
import json
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import resolve_config
from .evals import EVIDENCE_KINDS, EvalOutcome, as_outcome, score_case
from .pipeline import BasePipeline

SECRET_PATTERNS: List[Tuple[str, 're.Pattern']] = [
    ('aws access key', re.compile(r'AKIA[0-9A-Z]{16}')),
    ('private key', re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----')),
    ('github token', re.compile(r'gh[pousr]_[A-Za-z0-9]{36}')),
    ('google api key', re.compile(r'AIza[0-9A-Za-z_\-]{35}')),
    ('anthropic key', re.compile(r'sk-ant-[A-Za-z0-9\-_]{20,}')),
    ('literal secret', re.compile(r'(?i)\b(api[_-]?key|password|secret)\s*[:=]\s*["\'][^"\'$\s{]{8,}["\']')),
]
SCAN_PATHS: Tuple[str, ...] = ('src', 'policies', 'knowledge', 'infra', 'evals', 'contracts')
SCAN_SUFFIXES = {'.py', '.yaml', '.yml', '.tf', '.tfvars', '.md', '.json', '.toml', '.java', '.properties',
                 '.cfg', '.ini', '.txt', '.sh', '.env'}
SKIP_DIRS = {'.git', '.venv', 'node_modules', 'target', '.terraform', '__pycache__', 'build', 'dist'}
NOT_CONFIGURED = 'NOT_CONFIGURED'


def scan_secrets(root, paths: Iterable[str] = SCAN_PATHS,
                 patterns: Sequence[Tuple[str, 're.Pattern']] = SECRET_PATTERNS) -> List[str]:
    """Secret-pattern hits under root/<paths>, as 'relative/path: pattern name'."""
    root = Path(root)
    hits = []
    for folder in paths:
        base = root / folder
        files = [base] if base.is_file() else (base.rglob('*') if base.is_dir() else [])
        for path in files:
            if not path.is_file() or SKIP_DIRS & set(path.relative_to(root).parts):
                continue
            if path.suffix not in SCAN_SUFFIXES and path.name not in ('.env', 'Dockerfile'):
                continue
            try:
                text = path.read_text(encoding='utf-8')
            except (UnicodeDecodeError, OSError):
                continue
            hits += [f'{path.relative_to(root).as_posix()}: {name}' for name, rx in patterns if rx.search(text)]
    return hits


def load_cases(path) -> List[Dict[str, Any]]:
    """An evaluation or red-team set: a JSON list of cases (objects), or an object whose
    values are lists (each case gets 'kind' = the key; a plain string becomes {'text': ...}).
    Raises ValueError, naming the problem, when the file is not such a set or is empty."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f'{path.name}: not readable JSON ({error})') from error
    groups = data.items() if isinstance(data, dict) else [(None, data)]
    cases: List[Dict[str, Any]] = []
    for kind, items in groups:
        if not isinstance(items, list):
            raise ValueError(f'{path.name}: {kind or "the set"} must be a list of cases')
        for item in items:
            case = dict(item) if isinstance(item, dict) else {'text': item} if isinstance(item, str) else None
            if case is None:
                raise ValueError(f'{path.name}: a case must be an object or a string, not {type(item).__name__}')
            if kind is not None:
                case.setdefault('kind', kind)
            cases.append(case)
    if not cases:
        raise ValueError(f'{path.name}: the set has no cases')
    return cases


class ProjectReleaseGate(BasePipeline):
    """A release gate for one project. Subclass it and implement _score_case (and _probe if
    the project has a red-team set); everything else works as is."""

    scan_paths: Tuple[str, ...] = SCAN_PATHS
    secret_patterns: Sequence[Tuple[str, 're.Pattern']] = SECRET_PATTERNS
    evaluation_set = 'evals/golden.json'
    redteam_set = 'evals/redteam.json'
    conformance_strict = True
    evidence_kind: Optional[str] = None      # v1.9.0: None = decided from the environment (see the module doc)
    rag_pipeline = None                      # v1.9.0: a BaseRAGPipeline for `expect: {grounded: ...}`

    def __init__(self, root='.', run_tests: bool = True, artifact_name: Optional[str] = None) -> None:
        super().__init__()
        self.root = Path(root).resolve()
        self.run_tests = run_tests
        self.required_artifact_matrix = [artifact_name or self.root.name]
        self.conformance_paths = (str(self.root / 'src'),)
        self.evidence: Dict[str, Any] = {}
        self.notes: Dict[str, str] = {}

    # ---- generic stages -------------------------------------------------------------
    def _run_static_security_scans(self, config, **kwargs) -> bool:
        hits = scan_secrets(self.root, self.scan_paths, self.secret_patterns)
        self.evidence['secrets'] = hits
        return not hits

    def _execute_test_suite(self, config, **kwargs) -> bool:
        if not self.run_tests:
            self.notes['test_execution'] = 'SKIPPED: the CI runs the tests in its own step'
            return True
        done = subprocess.run([sys.executable, '-m', 'pytest', '-q'], cwd=self.root, capture_output=True, text=True)
        self.evidence['tests'] = (done.stdout.strip().splitlines() or [done.stderr[-300:]])[-1]
        return done.returncode == 0

    def _verify_scaffold_contracts(self, config, **kwargs) -> bool:
        kwargs.setdefault('conformance_strict', self.conformance_strict)
        ok = super()._verify_scaffold_contracts(config, **kwargs)
        self.evidence['conformance'] = [str(f) for f in self.conformance_findings]
        return ok

    def _compile_artifact(self, artifact_name, config, **kwargs) -> bool:
        src = self.root / 'src'
        return not src.is_dir() or bool(compileall.compile_dir(str(src), quiet=1))

    def _execute_deployment_rollout(self, strategy, config, **kwargs) -> dict:
        return {'deployed': False, 'strategy': strategy,
                'note': 'no live deployment from the release gate; the CI deploys'}

    # ---- domain stages: one hook per case -------------------------------------------
    def _run_rag_eval(self, config, **kwargs) -> float:
        path = self.root / self.evaluation_set
        if not path.is_file():
            self.notes['ragas_gate'] = f'{NOT_CONFIGURED}: no {self.evaluation_set}'
            return 1.0
        rows = []
        trials = max(1, int(resolve_config('eval_trials', config, kwargs)))
        self.evidence['run'] = self._run_metadata(trials)
        for case in load_cases(path):
            outcomes = []
            for _ in range(trials):
                value = self._score_case(case)
                if not isinstance(value, EvalOutcome) and not 0.0 <= float(value) <= 1.0:
                    raise ValueError(f'_score_case returned {value}; a score is between 0 and 1')
                outcomes.append(as_outcome(value, case.get('threshold')))
            scores = [o.score for o in outcomes]
            row = {'case': case, 'score': sum(scores) / len(scores)}
            if trials > 1:
                row.update({'trials': trials, 'scores': scores, 'min': min(scores),
                            'stdev': round(statistics.pstdev(scores), 4),
                            'pass_at_k': any(o.passed if o.passed is not None else o.score >= 1.0
                                              for o in outcomes)})
            reasons = sorted({o.reason for o in outcomes if o.reason})
            if reasons:
                row['reasons'] = reasons
            rows.append(row)
        self.evidence['evaluation'] = rows
        return sum(r['score'] for r in rows) / len(rows)

    def _run_dynamic_security_checks(self, config, **kwargs) -> bool:
        path = self.root / self.redteam_set
        if not path.is_file():
            self.notes['dynamic_security'] = f'{NOT_CONFIGURED}: no {self.redteam_set}'
            return True
        failures = []
        self.evidence.setdefault('run', self._run_metadata(1))
        for case in load_cases(path):
            failures += [str(f) for f in (self._probe(case) or [])]
        self.evidence['redteam'] = failures
        return not failures

    def _run_metadata(self, trials: int) -> Dict[str, Any]:
        """What produced this evidence (v1.9.0): the kind, the profile and the model."""
        live = os.environ.get('E2A_SWAP_LIVE', '').lower() == 'true'
        kind = self.evidence_kind or ('live' if live else 'wiring')
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f'evidence_kind must be one of {EVIDENCE_KINDS}')
        return {'evidence_kind': kind, 'profile': os.environ.get('E2A_PROVIDER_PROFILE') or 'simulated',
                'model': os.environ.get('LLM_MODEL_ID') or None, 'trials': trials}

    def _run_case(self, case: Dict[str, Any]) -> Dict[str, Any]:
        """Protected hook (v1.9.0): run the service on case['input'] and return the final state.
        With it, cases that declare `expect` are scored without writing _score_case or _probe."""
        raise NotImplementedError('to score cases by their `expect`, implement _run_case(case): run the '
                                  'service on case["input"] and return its state')

    def _score_case(self, case: Dict[str, Any]):
        """Protected hook: score one evaluation case in [0, 1] (1 = the service got it right); a
        float or an e2a.evals.EvalOutcome. Default (v1.9.0): a case with `expect` is run with
        _run_case and scored by e2a.evals.score_case; any other case is refused (fail-closed)."""
        if case.get('expect'):
            return score_case(self._run_case(case), case['expect'], self.rag_pipeline)
        raise NotImplementedError('decide how a case is scored: implement _score_case(case), or give the '
                                  'case an `expect` block and implement _run_case(case)')

    def _probe(self, case: Dict[str, Any]) -> List[str]:
        """Protected hook: run one red-team case; return what went wrong (empty = it held).
        Default (v1.9.0): a case with `expect` is run and checked like an evaluation case."""
        if case.get('expect'):
            outcome = score_case(self._run_case(case), case['expect'], self.rag_pipeline)
            return [] if outcome.passed else [f"{case.get('id', 'case')}: {outcome.reason or 'failed'}"]
        raise NotImplementedError('decide how a red-team case is checked: implement _probe(case), or give the '
                                  'case an `expect` block and implement _run_case(case)')


def gate_report(gate: ProjectReleaseGate, report: Dict[str, Any]) -> Dict[str, Any]:
    """The run_pipeline() report with the gate's evidence and honest stage notes
    (NOT_CONFIGURED / SKIPPED instead of PASSED), without the message log."""
    out = {k: v for k, v in report.items() if k != 'message_log'}
    stages = dict(out.get('stages', {}))
    for stage, note in gate.notes.items():
        if stage in stages:
            stages[stage] = note
    out['stages'] = stages
    out['evidence'] = gate.evidence
    return out


def run_release_gate(argv: Optional[List[str]] = None, gate_class=ProjectReleaseGate) -> int:
    parser = argparse.ArgumentParser(description='Run the project release gate (E2A ProjectReleaseGate).')
    parser.add_argument('--root', default='.', help='the project root (default: the current directory)')
    parser.add_argument('--skip-tests', action='store_true', help='the CI ran the tests in its own step')
    parser.add_argument('--report', default='release-gate-report.json')
    parser.add_argument('--config', default='{"fleet_replica_count": 1}', help='run_pipeline config, as JSON')
    args = parser.parse_args(argv)
    gate = gate_class(root=args.root, run_tests=not args.skip_tests)
    report = gate_report(gate, gate.run_pipeline(json.loads(args.config)))
    Path(args.report).write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('status', 'stages', 'rag_eval_score') if k in report}, indent=2))
    if report['status'] != 'SUCCESS':
        print('FAILED:', report.get('error'))
    return 0 if report['status'] == 'SUCCESS' else 1


if __name__ == '__main__':
    sys.exit(run_release_gate())
