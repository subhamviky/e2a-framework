"""v1.7.0 (design note MP-05): messaging with exactly-once effects (N1), identity at ingress
(N3), the literal-secret rule (N2), and the seams the cloud profiles bind."""
import json
import uuid

import pytest

import e2a
from e2a import (BrokerMessage, ClaimMapIdentityResolver, IdempotentConsumer, IdentityRefused, InMemoryBroker,
                 PhysicalOutcome, bindings_from_profile)
from e2a.bindings import SEAMS, Profile
from e2a.port_contract import check_message_broker
from e2a_inmemory_adapters import InMemoryIdempotencyStore


def fresh():
    broker, queue = InMemoryBroker(), f'q-{uuid.uuid4().hex[:6]}'
    return broker, queue, queue


# ------------------------------------------------------------------ N1 messaging
def test_the_reference_broker_meets_the_messaging_contract():
    passed = check_message_broker(fresh, timeout=1.5)
    assert len(passed) == 25 and 'a lost acknowledgement does not repeat the effect' in passed


class Ledger(IdempotentConsumer):
    def __init__(self, broker, store, source):
        super().__init__(broker, store, source)
        self.posted = []

    def _handle(self, message, config, tenant_id, idempotency_key):
        if message.body.get('amount') is None:
            raise ValueError('no amount')
        if message.body.get('flaky') and not self.posted:
            self.posted.append('flaky-attempt')
            raise ConnectionError('ledger down')
        self.posted.append((tenant_id, message.body['amount']))
        return {'physical_success': True, 'outcome': PhysicalOutcome.COMMITTED}


def test_the_consumer_reports_every_settlement():
    broker, q, _ = fresh()
    broker.publish(q, {'amount': 5}, {'tenant_id': 't1', 'idempotency_key': 'a'})
    broker.publish(q, {'amount': 5}, {'tenant_id': 't1', 'idempotency_key': 'a'})      # duplicate
    broker.publish(q, {'x': 1}, {'tenant_id': 't1', 'idempotency_key': 'b'})            # poison
    broker.publish(q, {'amount': 7}, {'idempotency_key': 'c'})                          # no tenant
    consumer = Ledger(broker, InMemoryIdempotencyStore(), q)
    report = consumer.consume(config={'consumer_retry_delay_seconds': 0})
    assert report['received'] == 4 and report['processed'] == 1 and report['duplicates'] == 1
    assert report['dead_lettered'] == 2 and consumer.posted == [('t1', 5)]
    assert any('missing tenant_id' in r for r in report['dead_letter_reasons'])


def test_a_transient_failure_is_retried_and_the_same_key_with_another_body_is_a_conflict():
    broker, q, _ = fresh()
    broker.publish(q, {'amount': 9, 'flaky': True}, {'tenant_id': 't1', 'idempotency_key': 'k'})
    consumer = Ledger(broker, InMemoryIdempotencyStore(), q)
    first = consumer.consume(config={'consumer_retry_delay_seconds': 0})
    second = consumer.consume(config={'consumer_retry_delay_seconds': 0})
    assert first['retried'] == 1 and second['processed'] == 1 and consumer.posted[-1] == ('t1', 9)
    broker.publish(q, {'amount': 10}, {'tenant_id': 't1', 'idempotency_key': 'k'})
    third = consumer.consume()
    assert third['dead_lettered'] == 1 and 'CONFLICT' in third['dead_letter_reasons'][0]


def test_a_message_delivered_too_often_is_dead_lettered():
    broker, q, _ = fresh()
    broker.publish(q, {'amount': 1}, {'tenant_id': 't1', 'idempotency_key': 'z'})
    message = broker.receive(q)[0]
    broker.nack(q, message, 0)
    for _ in range(2):
        broker.nack(q, broker.receive(q)[0], 0)
    report = Ledger(broker, InMemoryIdempotencyStore(), q).consume(config={'consumer_max_deliveries': 3})
    assert report['dead_lettered'] == 1 and 'delivered 4 times' in report['dead_letter_reasons'][0]


def test_the_consumer_needs_a_source():
    with pytest.raises(ValueError, match='no source'):
        Ledger(InMemoryBroker(), InMemoryIdempotencyStore(), None).consume()


def test_a_broker_message_reads_its_routing_attributes():
    m = BrokerMessage('1', {}, {'tenant_id': 't9', 'idempotency_key': 'k9'})
    assert (m.tenant_id, m.idempotency_key) == ('t9', 'k9')


# ------------------------------------------------------------------ N3 identity
COGNITO = {'tenant_id': 'custom:tenant_id', 'subject': 'sub', 'roles': 'cognito:groups', 'issuer': 'iss'}


def test_the_tenant_comes_from_verified_claims_through_the_claim_map():
    resolver = ClaimMapIdentityResolver({'identity_claim_map': COGNITO,
                                         'identity_trusted_issuers': ['https://idp.example']})
    claims = {'custom:tenant_id': 'acme', 'sub': 'u-1', 'cognito:groups': ['approver'], 'iss': 'https://idp.example'}
    ident = resolver.resolve(claims, verified=True, request_tenant='acme')
    assert (ident.tenant_id, ident.subject, ident.roles) == ('acme', 'u-1', ['approver'])
    with pytest.raises(IdentityRefused, match='not verified'):
        resolver.resolve(claims, verified=False)
    with pytest.raises(IdentityRefused, match='another tenant'):
        resolver.resolve(claims, verified=True, request_tenant='globex')
    with pytest.raises(IdentityRefused, match='not trusted'):
        resolver.resolve({**claims, 'iss': 'https://evil.example'}, verified=True)
    with pytest.raises(IdentityRefused, match='no tenant'):
        resolver.resolve({'sub': 'u-1', 'iss': 'https://idp.example'}, verified=True)
    assert isinstance(IdentityRefused('x'), PermissionError)


def test_nested_claims_space_separated_roles_and_a_tenant_map():
    resolver = ClaimMapIdentityResolver({'identity_claim_map': {'tenant_id': 'tid', 'roles': 'scp'},
                                         'identity_tenant_map': {'9f1c-directory': 'acme'}})
    ident = resolver.resolve({'tid': '9f1c-directory', 'scp': 'orders.read orders.write'}, verified=True)
    assert ident.tenant_id == 'acme' and ident.roles == ['orders.read', 'orders.write']
    nested = ClaimMapIdentityResolver({'identity_claim_map': {'tenant_id': 'app_metadata.tenant'}})
    assert nested.resolve({'app_metadata': {'tenant': 'globex'}}, verified=True).tenant_id == 'globex'


# ------------------------------------------------------------------ N2 literal secrets; seams
def test_a_literal_secret_in_a_profile_is_found_and_fails_the_gate(tmp_path):
    (tmp_path / 'base.json').write_text(json.dumps({'name': 'base', 'provider': 'x', 'config': {
        'auth_token': 'sk-literal-value', 'timeout': 9}}))
    (tmp_path / 'child.json').write_text(json.dumps({'name': 'child', 'provider': 'x', 'extends': str(tmp_path / 'base.json'),
                                                     'config': {'temperature': 0.1}}))
    (tmp_path / 'clean.json').write_text(json.dumps({'name': 'clean', 'provider': 'x', 'extends': str(tmp_path / 'base.json'),
                                                     'config': {'auth_token': '${TOOL_AUTH_TOKEN}'}}))
    assert Profile.load(str(tmp_path / 'base.json')).literal_secrets == ['auth_token']
    assert Profile.load(str(tmp_path / 'child.json')).literal_secrets == ['auth_token']     # inherited
    assert Profile.load(str(tmp_path / 'clean.json')).literal_secrets == []                  # overridden by ${VAR}

    bindings = bindings_from_profile(Profile.load(str(tmp_path / 'child.json')))
    bindings.verify()
    assert any('auth_token' in w for w in bindings.warnings)

    class Gate(e2a.BasePipeline):
        conformance_paths = ()

        def _run_static_security_scans(self, config, **kwargs):
            return True

        def _execute_test_suite(self, config, **kwargs):
            return True

        def _run_rag_eval(self, config, **kwargs):
            return 1.0

        def _compile_artifact(self, artifact_name, config, **kwargs):
            return True

        def _run_dynamic_security_checks(self, config, **kwargs):
            return True

        def _execute_deployment_rollout(self, strategy, config, **kwargs):
            return {}

    report = Gate().run_pipeline({'secret_scan_paths': []}, bindings=bindings)
    assert report['status'] == 'FAILED' and 'literal value' in report['error']


def test_the_infrastructure_seams_reach_the_config():
    for seam in ('message_broker', 'outbox_store', 'identity_resolver', 'transactional_store', 'lease'):
        assert seam in SEAMS
    broker = InMemoryBroker()
    b = bindings_from_profile(Profile(name='p', provider='x'), overrides={'message_broker': broker})
    assert b.config()['message_broker'] is broker
