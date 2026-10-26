"""Offline regressions for Groq run 26 (no external service needed)."""
import asyncio
from pathlib import Path
from sanad.agents import Pipeline
from sanad.config import ROOT, Config, Draft, Post, Slot
from sanad.skills import entry_evidence, strip_inline_evidence_markers


def test_verified_inline_labels_removed_but_unverified_labels_preserved():
    evidence = {'kb:SS-03738:description': 'Finding content'}
    assert (strip_inline_evidence_markers(
        'A grounded sentence.【kb:SS-03738:description】', evidence)
        == 'A grounded sentence.')
    assert '【kb:FAKE:description】' in strip_inline_evidence_markers(
        'An unsupported marker.【kb:FAKE:description】', evidence)
    assert '🎉' in strip_inline_evidence_markers('Do not silently delete 🎉', evidence)


def test_writer_receives_effective_seven_post_limit_and_strips_inline_labels(tmp_path):
    cfg = Config(ROOT)
    slot = Slot(id='thread_2', type='tech-thread', focus='Oracle Manipulation', source_mode='kb')
    evidence = entry_evidence(cfg.kb_by_id['SS-03738'])

    class Provider:
        async def generate(self, role, instructions, payload, schema, slot_id):
            assert role == 'writer'
            assert payload['post_type']['max_posts'] == 7
            assert 'between 5 and 7 posts' in instructions
            self.received = payload
            posts = [Post(text='A factual statement.【kb:SS-03738:description】',
                          evidence_ids=['kb:SS-03738:description']) for _ in range(5)]
            return Draft(posts=posts)

    provider = Provider()
    pipeline = Pipeline(cfg, provider, None, Path(tmp_path))
    state = {
        'slot': slot.model_dump(), 'topic': slot.focus,
        'research': {'source_ids': ['SS-03738']},
        'evidence': evidence, 'attempts': 0,
    }
    result = asyncio.run(pipeline.write(state))
    assert all(post['text'].startswith('A factual statement.') for post in result['draft']['posts'])
    assert all('【' not in post['text'] for post in result['draft']['posts'])
    assert all(post['evidence_ids'] == ['kb:SS-03738:description']
               for post in result['draft']['posts'])
