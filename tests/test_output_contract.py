"""Offline regressions for the supplied run's broken JSON fallback."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from sanad.config import Pick
from sanad.providers import LiveProvider, ProviderFailure


@pytest.mark.parametrize('valid', [True, False])
def test_json_fallback_receives_full_schema_and_never_invents_fields(valid):
    class InvalidJSON(Exception):
        status_code = 400
        code = 'json_validate_failed'

    captured = []

    class FakeLLM:
        def with_structured_output(self, schema, method, include_raw):
            async def invoke(messages):
                captured.append((method, messages))
                if method == 'json_schema':
                    raise InvalidJSON()
                value = {'source_ids': ['web:test:1']}
                if valid:
                    value['reason'] = 'The candidate matches the requested topic.'
                return {'raw': SimpleNamespace(content=json.dumps(value), usage_metadata={}), 'parsed': None}
            return SimpleNamespace(ainvoke=invoke)

    provider = LiveProvider({'OPENAI_API_KEY': 'test-only', 'LLM_TOKENS_PER_MINUTE': '0'})
    provider._llm_for = lambda role: FakeLLM()
    call = provider.generate('research', 'Select a source.', {'candidates': []}, Pick, 'm')
    if valid:
        assert asyncio.run(call).source_ids == ['web:test:1']
    else:
        with pytest.raises(ProviderFailure):
            asyncio.run(call)
        assert provider.calls[-1]['validation_problems'] == ['reason: missing']
    for _, messages in captured:
        contract = messages[0].content.split('complete JSON Schema:\n', 1)[1]
        parsed, _ = json.JSONDecoder().raw_decode(contract)
        assert parsed == Pick.model_json_schema()
    assert len(captured) == (2 if valid else 3)
