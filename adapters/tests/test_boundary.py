"""v1.6.1 (design note MP-04 / MP-03 section 0): e2a-adapters holds cloud infrastructure
(databases; brokers, secrets and identity from 1.7.0) and never a model provider. AI vendor
handling lives in the provider layer (e2a-managed-providers)."""
import ast
from pathlib import Path

import e2a_adapters

PACKAGE = Path(e2a_adapters.__file__).parent
AI_MODULES = ('anthropic', 'openai', 'vertexai', 'llama_stack_client', 'cohere', 'mistralai', 'langchain',
              'llama_index', 'litellm', 'ollama')
AI_SUBMODULES = ('google.genai', 'google.generativeai', 'google.cloud.aiplatform', 'azure.ai',
                 'azure.search', 'azure.identity.aio._internal.ai')
AI_SERVICE_CLIENTS = ('bedrock', 'bedrock-runtime', 'bedrock-agent', 'bedrock-agent-runtime', 'sagemaker-runtime')


def test_no_model_provider_sdk_in_adapters():
    modules = sorted(PACKAGE.rglob('*.py'))
    assert modules, PACKAGE
    found = []
    for path in modules:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name.split('.')[0] in AI_MODULES or name.startswith(AI_SUBMODULES):
                    found.append(f'{path.name}:{node.lineno}: import {name}')
            if isinstance(node, ast.Constant) and node.value in AI_SERVICE_CLIENTS:
                found.append(f'{path.name}:{node.lineno}: AI service client {node.value!r}')
    assert not found, 'model-provider code in e2a-adapters:\n' + '\n'.join(found)
