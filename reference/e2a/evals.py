"""E2A: evaluation (v1.9.0, hub design note EV-01).

One shape for every evaluation result, scorers every project can reuse, an online-evaluation
port, a calibration report and the promotion of production records into candidate cases.

  EvalOutcome          score, passed, threshold, reason, grader, evidence. BaseAgent._evaluate_output
                       may return one instead of a float; it is recorded in state['eval_outcome'] and
                       in the message log (event 'evaluation': eval.score, eval.threshold, ...)
  scorers              exact, contains, schema, refusal, no_pii, grounded, trajectory; score_case()
                       combines the checks a case declares under `expect` (the lowest score wins)
  BaseOnlineEvaluator  the port. BaseWorkflow.execute() submits a record of a finished, settled run
                       when config['online_evaluator'] is bound; sampled (online_eval_sample_rate),
                       budgeted (online_eval_max_per_minute), never on the critical path: a failing
                       evaluator is logged, never raised
    RecordingOnlineEvaluator   JSON lines, for CI and the landscape demo
    OutboxOnlineEvaluator      the record goes to the outbox (BaseOutboxStore); a consumer scores it
    ScoringOnlineEvaluator     scores in a background thread with the scorers, writes to a sink
  calibration_report   a scorer or judge against human labels: agreement, Cohen's kappa, correlation,
                       and the threshold that best matches the labels
  promote_candidates   low-score, fallback and refusal records -> redacted candidate cases with
                       provenance, for a person to review (never written into a golden set directly)

    python -m e2a.evals promote --records online.jsonl --out evals/candidates.json [--below 0.7]
    python -m e2a.evals calibrate --labels labels.json
"""
import argparse
import hashlib
import json
import math
import re
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from .schema_check import schema_errors

__all__ = ['EvalOutcome', 'as_outcome', 'exact', 'contains', 'schema', 'refusal', 'no_pii', 'grounded',
           'trajectory', 'score_case', 'BaseOnlineEvaluator', 'RecordingOnlineEvaluator',
           'OutboxOnlineEvaluator', 'ScoringOnlineEvaluator', 'run_record', 'submit_online',
           'calibration_report', 'promote_candidates', 'redact', 'EVIDENCE_KINDS']

EVIDENCE_KINDS = ('wiring', 'quality', 'live')
_PII = [('email', re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')),
        ('ssn', re.compile(r'\b\d{3}-\d{2}-\d{4}\b')),
        ('card', re.compile(r'(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])')),
        ('phone', re.compile(r'(?<![\w-])\+\d{1,3}[ -]?\(?\d{2,4}\)?[ -]?\d{3,4}[ -]?\d{3,4}(?![\w-])')),
        ('bearer', re.compile(r'Bearer\s+[A-Za-z0-9\-_.=]{16,}'))]


# ------------------------------------------------------------------ the result
@dataclass
class EvalOutcome:
    """One evaluation: a score in [0, 1], whether it passed, against which threshold, why, by
    which grader, and the evidence (what was compared)."""
    score: float
    passed: Optional[bool] = None
    threshold: Optional[float] = None
    reason: str = ''
    grader: str = ''
    evidence: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.score = float(self.score)
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f'a score is between 0 and 1, not {self.score}')
        if self.passed is None and self.threshold is not None:
            self.passed = self.score >= self.threshold

    def __float__(self) -> float:
        return self.score

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def as_outcome(value: Any, threshold: Optional[float] = None, grader: str = '') -> EvalOutcome:
    """A float (v1.1 to v1.8) or an EvalOutcome, as an EvalOutcome with the threshold applied."""
    if isinstance(value, EvalOutcome):
        if threshold is not None and value.threshold is None:
            value.threshold, value.passed = threshold, value.score >= threshold
        value.grader = value.grader or grader
        return value
    return EvalOutcome(float(value or 0.0), threshold=threshold, grader=grader)


def _text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ('text', 'answer', 'content'):
            if isinstance(value.get(key), str):
                return value[key]
        return json.dumps(value, sort_keys=True, default=str)
    return '' if value is None else str(value)


def _norm(text: str) -> str:
    return ' '.join(re.sub(r'[^\w\s-]', ' ', text.lower()).split())


# ------------------------------------------------------------------ scorers
def exact(output: Any, expected: Any, normalize: bool = True) -> EvalOutcome:
    got, want = _text(output), _text(expected)
    same = _norm(got) == _norm(want) if normalize else got == want
    return EvalOutcome(1.0 if same else 0.0, passed=same, grader='exact',
                       reason='' if same else 'the output differs from the expected one',
                       evidence={'expected': want[:200], 'got': got[:200]})


def contains(output: Any, required: Sequence[str], forbidden: Sequence[str] = ()) -> EvalOutcome:
    """Every required phrase present (citations, ids, key facts), no forbidden one."""
    text = _norm(_text(output))
    missing = [r for r in required if _norm(r) not in text]
    present = [f for f in forbidden if _norm(f) in text]
    total = len(required) + len(forbidden)
    score = 1.0 if total == 0 else (total - len(missing) - len(present)) / total
    ok = not missing and not present
    return EvalOutcome(score, passed=ok, grader='contains', evidence={'missing': missing, 'forbidden': present},
                       reason='' if ok else f'missing {missing}' + (f', forbidden {present}' if present else ''))


def schema(output: Any, json_schema: Dict[str, Any]) -> EvalOutcome:
    value = output
    if isinstance(output, dict) and isinstance(output.get('text'), str):
        value = output['text']
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return EvalOutcome(0.0, passed=False, grader='schema', reason='the output is not JSON')
    errors = schema_errors(json_schema, value)
    return EvalOutcome(0.0 if errors else 1.0, passed=not errors, grader='schema',
                       reason='; '.join(errors[:3]), evidence={'errors': errors[:10]})


def refusal(state: Mapping[str, Any], expected: bool = True) -> EvalOutcome:
    """Was the request refused (a governance refusal, a model refusal or a fallback)?"""
    refused = bool(state.get('refusal') or state.get('model_refusal') or state.get('evaluation') == 'FALLBACK'
                   or state.get('status') in ('REFUSED',))
    ok = refused == bool(expected)
    return EvalOutcome(1.0 if ok else 0.0, passed=ok, grader='refusal',
                       reason='' if ok else ('expected a refusal' if expected else 'refused unexpectedly'),
                       evidence={'refusal': state.get('refusal') or state.get('model_refusal') or None})


def no_pii(output: Any, patterns: Optional[Sequence] = None) -> EvalOutcome:
    text = _text(output)
    hits = [name for name, pattern in (patterns or _PII) if pattern.search(text)]
    return EvalOutcome(0.0 if hits else 1.0, passed=not hits, grader='no_pii',
                       reason=f'personal data in the output: {hits}' if hits else '', evidence={'kinds': hits})


def grounded(answer: Any, retrieved: Sequence[Dict[str, Any]], pipeline=None, threshold: float = 0.85,
             config: Optional[Dict[str, Any]] = None) -> EvalOutcome:
    """Groundedness through the project's BaseRAGPipeline.evaluate_answer()."""
    if pipeline is None:
        return EvalOutcome(0.0, passed=False, grader='grounded', reason='no RAG pipeline to score with')
    score = float(pipeline.evaluate_answer(_text(answer), list(retrieved or []), config or {}))
    return EvalOutcome(max(0.0, min(1.0, score)), threshold=threshold, grader='grounded')


def trajectory(state: Mapping[str, Any], expect: Mapping[str, Any]) -> EvalOutcome:
    """The path a run took (state['trajectory'], state['steps']) against what a case expects:
      tools_required, tools_forbidden   names
      tools_order                       names that must appear in this relative order
      max_tool_calls                    an upper bound
      args                              {tool: {field: value}} on at least one call of that tool
      steps_required                    workflow step names that must have run (status DONE)
    """
    calls = [t for t in state.get('trajectory') or [] if t.get('kind') == 'tool']
    names = [c['tool'] for c in calls]
    problems: List[str] = []
    for tool in expect.get('tools_required', []):
        if tool not in names:
            problems.append(f'{tool} was not called')
    for tool in expect.get('tools_forbidden', []):
        if tool in names:
            problems.append(f'{tool} was called but is forbidden')
    order = list(expect.get('tools_order', []))
    if order:
        positions = [names.index(t) if t in names else -1 for t in order]
        if -1 in positions or positions != sorted(positions):
            problems.append(f'tools not called in the order {order}')
    limit = expect.get('max_tool_calls')
    if limit is not None and len(calls) > int(limit):
        problems.append(f'{len(calls)} tool calls, more than {limit}')
    for tool, fields in (expect.get('args') or {}).items():
        if not any(c['tool'] == tool and all(c.get('args', {}).get(k) == v for k, v in fields.items())
                   for c in calls):
            problems.append(f'no call of {tool} with {fields}')
    done = {s.get('step') for s in state.get('steps') or [] if s.get('status') == 'DONE'}
    for step in expect.get('steps_required', []):
        if step not in done:
            problems.append(f'step {step} did not complete')
    checks = sum(len(expect.get(k, [])) for k in ('tools_required', 'tools_forbidden', 'steps_required')) \
        + (1 if order else 0) + (1 if limit is not None else 0) + len(expect.get('args') or {})
    score = 1.0 if checks == 0 else max(0.0, (checks - len(problems)) / checks)
    return EvalOutcome(score, passed=not problems, grader='trajectory', reason='; '.join(problems),
                       evidence={'tools': names, 'steps': sorted(s for s in done if s)})


def score_case(state: Mapping[str, Any], expect: Mapping[str, Any], pipeline=None,
               config: Optional[Dict[str, Any]] = None) -> EvalOutcome:
    """Every check a case declares, on the state a run produced; the lowest score wins, and the
    case passes only if every check passes. An empty `expect` fails: nothing was decided."""
    if not expect:
        return EvalOutcome(0.0, passed=False, grader='case', reason='the case declares no expect')
    output = state.get('response')
    parts: List[EvalOutcome] = []
    if 'exact' in expect:
        parts.append(exact(output, expect['exact']))
    if 'contains' in expect or 'forbidden' in expect:
        parts.append(contains(output, expect.get('contains', []), expect.get('forbidden', [])))
    if 'schema' in expect:
        parts.append(schema(output, expect['schema']))
    if 'refusal' in expect:
        parts.append(refusal(state, bool(expect['refusal'])))
    if expect.get('no_pii'):
        parts.append(no_pii(output))
    if 'grounded' in expect:
        parts.append(grounded(output, state.get('retrieved') or [], pipeline, float(expect['grounded']), config))
    if 'trajectory' in expect:
        parts.append(trajectory(state, expect['trajectory']))
    unknown = set(expect) - {'exact', 'contains', 'forbidden', 'schema', 'refusal', 'no_pii', 'grounded',
                             'trajectory', 'threshold'}
    if unknown:
        parts.append(EvalOutcome(0.0, passed=False, grader='case', reason=f'unknown checks {sorted(unknown)}'))
    if not parts:
        return EvalOutcome(0.0, passed=False, grader='case', reason='the case declares no known check')
    worst = min(parts, key=lambda p: p.score)
    ok = all(p.passed is not False for p in parts)
    return EvalOutcome(worst.score, passed=ok, threshold=expect.get('threshold'), grader='case',
                       reason='; '.join(p.reason for p in parts if p.reason),
                       evidence={p.grader: p.to_dict() for p in parts})


# ------------------------------------------------------------------ redaction
def redact(value: Any) -> Any:
    """Personal data and credentials replaced, recursively (records leaving the process)."""
    if isinstance(value, str):
        for name, pattern in _PII:
            value = pattern.sub(f'[{name}]', value)
        return value
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


# ------------------------------------------------------------------ online evaluation
def run_record(state: Mapping[str, Any], workflow: str = '') -> Dict[str, Any]:
    """What an online evaluator gets about a finished run: identifiers, the evaluation, the path,
    the outcome and the answer, redacted. Never the message log or credentials."""
    return {
        'time': time.time(), 'workflow': workflow,
        'tenant_id': state.get('tenant_id'), 'correlation_id': state.get('correlation_id'),
        'idempotency_key': state.get('idempotency_key'), 'status': state.get('status'),
        # free text is redacted; identifiers are kept so a record can be traced
        'query': redact(state.get('query') or state.get('text')), 'response': redact(state.get('response')),
        'evaluation': state.get('evaluation'), 'eval_outcome': redact(state.get('eval_outcome')),
        'trajectory': redact(state.get('trajectory') or []), 'steps': state.get('steps') or [],
        'outcome': state.get('outcome'), 'refusal': redact(state.get('refusal')),
        'failure': state.get('failure'), 'failed': bool(state.get('failed_keys')),
    }


class BaseOnlineEvaluator(ABC):
    """The online-evaluation port. submit() must return quickly: it runs after the request has
    settled, on the request's thread. Heavy scoring happens elsewhere (a background thread, an
    outbox consumer, a managed evaluation service)."""

    def __init__(self, max_per_minute: Optional[int] = None) -> None:
        self.max_per_minute = max_per_minute
        self._window, self._count, self._lock = 0, 0, threading.Lock()

    def admit(self, limit: Optional[int] = None) -> bool:
        limit = self.max_per_minute if self.max_per_minute is not None else limit
        if not limit:
            return True
        with self._lock:
            minute = int(time.time() // 60)
            if minute != self._window:
                self._window, self._count = minute, 0
            if self._count >= int(limit):
                return False
            self._count += 1
            return True

    @abstractmethod
    def submit(self, record: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> None:
        """Take one run record. Must not block the caller for long, and must not raise for a
        scoring problem (log it); the workflow logs and swallows anything it raises."""


class RecordingOnlineEvaluator(BaseOnlineEvaluator):
    """Appends each record as one JSON line (CI, the landscape demo, promotion input)."""

    def __init__(self, path, max_per_minute: Optional[int] = None) -> None:
        super().__init__(max_per_minute)
        self.path = Path(path)
        self._write = threading.Lock()

    def submit(self, record, config=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write, self.path.open('a', encoding='utf-8') as out:
            out.write(json.dumps(record, sort_keys=True, default=str) + '\n')


class OutboxOnlineEvaluator(BaseOnlineEvaluator):
    """Puts the record in the outbox, for a consumer to score (D2: through the outbox when bound)."""

    def __init__(self, outbox, topic: str = 'e2a.evaluation', max_per_minute: Optional[int] = None) -> None:
        super().__init__(max_per_minute)
        self.outbox, self.topic = outbox, topic

    def submit(self, record, config=None):
        self.outbox.add({'destination': self.topic, 'body': json.dumps(record, sort_keys=True, default=str),
                         'attributes': {'kind': 'e2a.run_record', 'workflow': record.get('workflow') or ''},
                         'tenant_id': record.get('tenant_id'),
                         'idempotency_key': f"eval:{record.get('correlation_id') or ''}"}, config)


class ScoringOnlineEvaluator(BaseOnlineEvaluator):
    """Scores each record with `scorers` (callables: record -> EvalOutcome) in a background thread
    and hands {record ids, outcomes} to `sink` (a callable or another evaluator)."""

    def __init__(self, scorers: Sequence[Callable[[Dict[str, Any]], EvalOutcome]], sink,
                 background: bool = True, max_per_minute: Optional[int] = None) -> None:
        super().__init__(max_per_minute)
        self.scorers, self.sink, self.background = list(scorers), sink, background

    def _score(self, record):
        results = []
        for scorer in self.scorers:
            try:
                results.append(as_outcome(scorer(record)).to_dict())
            except Exception as error:                      # a scorer bug never reaches the caller
                results.append(EvalOutcome(0.0, passed=False, grader=getattr(scorer, '__name__', 'scorer'),
                                           reason=f'scorer failed: {error}').to_dict())
        scored = {'correlation_id': record.get('correlation_id'), 'tenant_id': record.get('tenant_id'),
                  'workflow': record.get('workflow'), 'outcomes': results,
                  'score': min((r['score'] for r in results), default=None)}
        (self.sink.submit(scored) if isinstance(self.sink, BaseOnlineEvaluator) else self.sink(scored))

    def submit(self, record, config=None):
        if self.background:
            threading.Thread(target=self._score, args=(record,), daemon=True).start()
        else:
            self._score(record)


def _sampled(key: str, rate: float) -> bool:
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    digest = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
    return digest / 0xFFFFFFFF < rate


def submit_online(state: Mapping[str, Any], config: Optional[Dict[str, Any]], workflow: str = '',
                  log: Optional[Callable[..., None]] = None) -> bool:
    """Called by BaseWorkflow.execute() after the claim is settled. Returns True if submitted."""
    from .config import resolve_config
    evaluator = (config or {}).get('online_evaluator')
    if not isinstance(evaluator, BaseOnlineEvaluator):
        return False
    key = str(state.get('correlation_id') or state.get('idempotency_key') or '')
    if not _sampled(key, float(resolve_config('online_eval_sample_rate', config))):
        return False
    if not evaluator.admit(resolve_config('online_eval_max_per_minute', config)):
        return False
    try:
        evaluator.submit(run_record(state, workflow), config)
        return True
    except Exception as error:                              # never on the critical path
        if log:
            log('online_evaluation_failed', error=str(error)[:300])
        return False


# ------------------------------------------------------------------ calibration
def calibration_report(cases: Sequence[Mapping[str, Any]], threshold: float = 0.5) -> Dict[str, Any]:
    """Each case: {'score': grader score in [0,1], 'label': human verdict (true = good)}.
    Agreement and Cohen's kappa at `threshold`, Pearson correlation, and the threshold in
    0.05 steps that agrees best with the labels."""
    rows = [(float(c['score']), bool(c['label'])) for c in cases]
    if not rows:
        raise ValueError('no labelled cases')

    def agree(t):
        tp = sum(1 for s, l in rows if s >= t and l)
        tn = sum(1 for s, l in rows if s < t and not l)
        fp = sum(1 for s, l in rows if s >= t and not l)
        fn = sum(1 for s, l in rows if s < t and l)
        n = len(rows)
        po = (tp + tn) / n
        pe = ((tp + fp) * (tp + fn) + (tn + fn) * (tn + fp)) / (n * n)
        kappa = 1.0 if pe == 1 else (po - pe) / (1 - pe)
        return {'threshold': round(t, 2), 'agreement': round(po, 4), 'kappa': round(kappa, 4),
                'false_pass': fp, 'false_fail': fn}

    xs, ys = [s for s, _ in rows], [1.0 if l else 0.0 for _, l in rows]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    corr = 0.0 if not sx or not sy else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)
    best = max((agree(t / 20) for t in range(1, 20)), key=lambda r: (r['kappa'], r['agreement']))
    return {'cases': len(rows), 'at_threshold': agree(threshold), 'correlation': round(corr, 4),
            'suggested_threshold': best['threshold'], 'at_suggested': best}


# ------------------------------------------------------------------ promotion
def promote_candidates(records: Iterable[Mapping[str, Any]], below: float = 0.7,
                       source: str = '') -> List[Dict[str, Any]]:
    """Records worth a regression case (a low score, a fallback, a refusal, a failure) as candidate
    cases: redacted input, the observed behaviour, an empty `expect` for a person to fill in, and
    provenance. A candidate with an empty `expect` fails the gate until reviewed: by design."""
    out, seen = [], set()
    for r in records:
        score = (r.get('eval_outcome') or {}).get('score') if isinstance(r.get('eval_outcome'), dict) else None
        score = r.get('score', score)
        why = [w for w, hit in (('low_score', score is not None and score < below),
                                ('fallback', r.get('evaluation') == 'FALLBACK'),
                                ('refusal', bool(r.get('refusal'))),
                                ('failure', bool(r.get('failed') or r.get('failure')))) if hit]
        query = r.get('query')
        if not why or not query:
            continue
        fingerprint = hashlib.sha256(json.dumps(redact(query), sort_keys=True, default=str).encode()).hexdigest()[:12]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append({'id': f'candidate-{fingerprint}', 'input': {'query': redact(query)}, 'expect': {},
                    'observed': redact({'response': r.get('response'), 'score': score, 'tools': [
                        t.get('tool') for t in r.get('trajectory') or [] if t.get('kind') == 'tool']}),
                    'provenance': {'source': source, 'correlation_id': r.get('correlation_id'),
                                   'tenant_id': r.get('tenant_id'), 'reasons': why,
                                   'status': 'NEEDS_REVIEW'}})
    return out


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def run_evals_cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m e2a.evals')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('promote', help='records -> candidate cases for review')
    p.add_argument('--records', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--below', type=float, default=0.7)
    c = sub.add_parser('calibrate', help='grader scores against human labels')
    c.add_argument('--labels', required=True, help='JSON list of {score, label}')
    c.add_argument('--threshold', type=float, default=0.5)
    args = parser.parse_args(argv)
    if args.command == 'promote':
        records = _read_jsonl(Path(args.records))
        found = promote_candidates(records, args.below, source=args.records)
        out = Path(args.out)
        existing = json.loads(out.read_text(encoding='utf-8')) if out.is_file() else []
        ids = {e.get('id') for e in existing}
        merged = existing + [f for f in found if f['id'] not in ids]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(merged, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'records': len(records), 'candidates': len(found),
                          'new': len(merged) - len(existing), 'file': str(out)}))
    else:
        print(json.dumps(calibration_report(json.loads(Path(args.labels).read_text(encoding='utf-8')),
                                            args.threshold), indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(run_evals_cli())
