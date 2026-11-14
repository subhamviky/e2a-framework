"""v1.6.1 (design note MP-04 / MP-03 section 0): E2A core stays vendor-neutral.

1. No module of the `e2a` package imports a vendor SDK (model providers or clouds); vendor
   code lives in provider packages (e2a-managed-providers) and in e2a-adapters.
2. No code compares a value with a vendor or provider name: behaviour is selected by config
   keys, capabilities and the `features` map, never by `if provider == "<vendor>"`.
"""
import ast
from pathlib import Path

import e2a

PACKAGE = Path(e2a.__file__).parent
VENDOR_MODULES = ('anthropic', 'openai', 'google', 'vertexai', 'boto3', 'botocore', 'aioboto3', 'azure',
                  'llama_stack_client', 'cohere', 'mistralai', 'langchain', 'llama_index', 'litellm', 'ollama')
VENDOR_NAMES = {'anthropic', 'claude', 'openai', 'gpt', 'azure', 'azure_openai', 'foundry', 'bedrock', 'aws',
                'vertex', 'vertexai', 'gemini', 'google', 'gcp', 'llama', 'meta', 'llama_stack', 'ollama',
                'cohere', 'mistral'}


def _modules():
    modules = sorted(p for p in PACKAGE.rglob('*.py'))
    assert any(p.name == 'llm.py' for p in modules), f'scanning the wrong package: {PACKAGE}'
    return modules


def test_no_vendor_sdk_imports():
    found = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name.split('.')[0] in VENDOR_MODULES:
                    found.append(f'{path.name}:{node.lineno}: import {name}')
    assert not found, 'vendor SDK imported by core:\n' + '\n'.join(found)


def test_no_branching_on_a_vendor_name():
    found = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Compare):
                continue
            for operand in [node.left, *node.comparators]:
                values = operand.elts if isinstance(operand, (ast.Tuple, ast.List, ast.Set)) else [operand]
                for v in values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str) and v.value.lower() in VENDOR_NAMES:
                        found.append(f'{path.name}:{node.lineno}: compares with {v.value!r}')
    assert not found, 'provider-name branching in core:\n' + '\n'.join(found)
