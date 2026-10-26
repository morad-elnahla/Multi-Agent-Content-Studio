"""Regressions from live run 3: every case below was a real rejection or crash in that run."""
import asyncio
import json
import httpx
import pytest
from pydantic import ValidationError
from sanad.config import ROOT, Config, Claim, Citation, Draft, Judgment, Post, Scores
from sanad.quality import evaluate, style_checks, tweet_length
from sanad.research import body_relevant, noise_url, relevant_result
from sanad.skills import clean_evidence_text, entry_evidence, verbatim_quote_choices

CFG = Config(ROOT)
KB = {e['id']: e for e in CFG.kb}


def slot(**kw):
    from sanad.config import Slot
    base = dict(id='s', type='marketing-post', service='agentic-security', format='short-tip', source_mode='web')
    return Slot(**{**base, **kw})


def test_evidence_is_plain_prose_without_markdown_or_code():
    ev = entry_evidence(KB['SS-03699'])
    text = ev['kb:SS-03699:description']
    assert '**' not in text and '`' not in text and 'fn test_' not in text and '#[test' not in text
    rec = entry_evidence(KB['SS-03738'])['kb:SS-03738:recommendation']
    assert rec.startswith('Use a canonical feed account') and '- ' not in rec


def test_two_sentence_claim_has_one_exact_quote_candidate():
    text = entry_evidence(KB['SS-03699'])['kb:SS-03699:description']
    claim = 'A user with TokenLimit can submit SetAuthority on the token account and gain ownership of it.'
    options = verbatim_quote_choices(text, claim)
    assert any('SetAuthority' in o and 'gains ownership' in o for o in options)
    assert all(o in text for o in options)           # still exact substrings


def test_judge_can_combine_quotes_and_each_must_be_exact():
    draft = Draft(posts=[Post(text='Sentence about a thing.', evidence_ids=['e1', 'e2'])])
    ev = {'e1': 'First exact passage in the source text.', 'e2': 'Second exact passage in the source text.'}
    def judged(extra_quote):
        claim = Claim(unit_id='p1s1', supported=True, evidence_id='e1', quote='First exact passage in the source',
                      reason='ok', extra_citations=[Citation(evidence_id='e2', quote=extra_quote)])
        return Judgment(approve=True, scores=Scores(accuracy=4, usefulness=4, clarity=4, brand_fit=4), claims=[claim], reasons=[])
    assert evaluate(judged('Second exact passage in the source'), draft, ev, CFG.style)['accepted']
    bad = evaluate(judged('Invented second passage'), draft, ev, CFG.style)
    assert not bad['accepted'] and any('not an exact source substring' in r for r in bad['reasons'])


def test_link_counts_as_23_characters_and_message_says_how_much():
    url = 'https://immunefi.com/bug-bounty/zksync-os/information'
    assert tweet_length('x' * 200 + ' ' + url) == 224
    draft = Draft(posts=[Post(text='y' * 270 + ' ' + url, evidence_ids=['web:agentic-security:1:body'])])
    errors, _ = style_checks(draft, slot(format='news-reaction'), CFG,
                             {'web:agentic-security:1:body': 'prompt injection agent'}, {url})
    assert any('exceeds 280' in e and 'over' in e for e in errors)
    ok = Draft(posts=[Post(text='y' * 240 + ' ' + url, evidence_ids=['web:agentic-security:1:body'])])
    errors, _ = style_checks(ok, slot(format='news-reaction'), CFG, {'web:agentic-security:1:body': 'prompt injection agent'}, {url})
    assert not any('exceeds 280' in e for e in errors)


def test_empty_evidence_ids_no_longer_crash_the_writer_call():
    post = Post(text='A fact.', evidence_ids=[])          # used to raise ValidationError and fail the slot
    errors, _ = style_checks(Draft(posts=[post]), slot(), CFG, {'web:agentic-security:1:body': 'x'}, set())
    assert any('evidence' in e for e in errors)


def test_marketing_overclaims_and_ids_inside_text_are_rejected():
    ev = {'web:agentic-security:1:body': 'prompt injection tool abuse agent', 'brand:approved_service': 'x'}
    draft = Draft(posts=[Post(text='SanadSquad secures the full stack. Prompt injection is a risk [web:agentic-security:1:body].',
                              evidence_ids=list(ev))])
    errors, _ = style_checks(draft, slot(), CFG, ev, set())
    assert any('service overclaim' in e for e in errors) and any('evidence IDs must go in evidence_ids' in e for e in errors)
    fine = Draft(posts=[Post(text='SanadSquad reviews prompts, tool permissions and spending limits.', evidence_ids=list(ev))])
    errors, _ = style_checks(fine, slot(), CFG, ev, set())
    assert not any('overclaim' in e for e in errors)


def test_irrelevant_pages_are_not_used_as_marketing_evidence():
    jobs = {'url': 'https://jobs.solana.com/companies/phantom/jobs/1', 'title': 'Staff SRE', 'content': 'security audit blockchain'}
    topics = {'url': 'https://github.com/topics/m-and-a', 'title': 'm-and-a', 'content': 'security audit'}
    assert noise_url(jobs['url'], CFG.sources.get('_deny_url_patterns')) and noise_url(topics['url'], CFG.sources.get('_deny_url_patterns'))
    bounty = {'url': 'https://immunefi.com/bug-bounty/zksync-os/information', 'title': 'ZKsync OS Bug Bounty | Immunefi',
              'content': 'This program rewards security audit findings for blockchain'}
    assert not relevant_result(bounty, 'web3-security', 'Web3 audit coverage')     # "security/audit" alone is not topical
    assert not body_relevant('Bug bounty rules and payouts. ' * 20, 'agentic-security', 'prompt injection')
    assert body_relevant('Prompt injection can reach tool abuse in an LLM agent. ' * 3, 'agentic-security', 'prompt injection')


def test_technical_thread_uses_exactly_one_finding():
    from sanad.agents import Pipeline
    import inspect
    assert '[:1]' in inspect.getsource(Pipeline.research)


def test_validation_error_gets_one_corrective_retry():
    from sanad.providers import LiveProvider
    import inspect
    source = inspect.getsource(LiveProvider.generate)
    assert 'validation_retry_done' in source and 'did not match the schema' in source


# ---------------- run 4: visual crash, 413, github junk
def test_allowlist_entries_can_be_limited_to_a_path_prefix():
    from sanad.research import host_allowed
    allow = ['github.com/ethereum', 'rekt.news']
    assert host_allowed('https://github.com/ethereum/solidity/releases', allow, [])
    assert not host_allowed('https://github.com/random-user/awesome-list', allow, [])
    assert not host_allowed('https://github.com/ethereumfake/x', allow, [])      # prefix must end at a path boundary
    assert host_allowed('https://rekt.news/some-post', allow, [])
    assert not any(e == 'github.com' for g in CFG.services.values() for v in g.values() for e in v)


def test_visual_agent_survives_a_wrong_source_id_and_gets_a_small_payload(tmp_path):
    from sanad.agents import Pipeline
    from sanad.config import VisualIdea
    from sanad.providers import DemoProvider
    from sanad.research import WebResearch, fixture_data
    seen = {}

    class Visual(DemoProvider):
        async def generate(self, role, prompt, payload, schema, slot):
            seen.update(payload)
            return VisualIdea(layout='flow', symbol='gate', source_id='SS-03740 title, not an evidence id')

    pipe = Pipeline(CFG, Visual(), WebResearch(CFG, 'demo', fixture=fixture_data()), tmp_path)
    big = 'long page text ' * 3000
    state = {'slot': slot(id='m').model_dump(), 'topic': 'Topic', 'evidence': {'web:a:1': big, 'web:a:2': big},
             'draft': {'posts': [{'text': 'A post.', 'evidence_ids': ['web:a:2']}]}}
    out = asyncio.run(pipe.visual(state))
    assert out['visual']['source_id'] == 'web:a:2'                     # mapped, not a crash
    assert len(json.dumps(seen)) < 3000 and 'evidence' not in seen     # previews only: no HTTP 413


def test_queries_are_short_and_news_reaction_is_not_limited_to_one_week():
    import inspect
    from sanad.research import WebResearch
    source = inspect.getsource(WebResearch.search)
    assert "'month'" in source and "security guidance or analysis" not in source


# ---------------- run 6: invisible space, invented benefit, over-strict relevance
def test_every_unicode_space_and_dash_is_normalised():
    from sanad.skills import clean_typography
    assert clean_typography('10\u202fk and 3\u2009x and front\u2011running \u2212 end') == '10 k and 3 x and front-running - end'
    assert clean_typography('a\u200bb\u00adc') == 'abc'
    assert clean_typography('emoji \U0001F680 stays').count('\U0001F680') == 1      # never silently strips emoji


def test_technical_sentence_cannot_add_a_benefit_the_source_does_not_state():
    from sanad.config import Slot
    ev = {'kb:SS-1:recommendation': 'If the actual output falls below the bound, the transaction reverts.',
          'kb:SS-1:description': 'x'}
    s = Slot(id='t', type='tech-thread', focus='x', source_mode='kb')
    bad = Draft(posts=[Post(text='If the output is below the bound, the transaction reverts, protecting the user from front-running.',
                            evidence_ids=['kb:SS-1:recommendation'])])
    errors, _ = style_checks(bad, s, CFG, ev, set())
    assert any('protecting ..." adds an effect' in e for e in errors)
    good = Draft(posts=[Post(text='If the output is below the bound, the transaction reverts.', evidence_ids=['kb:SS-1:recommendation'])])
    assert not any('adds an effect' in e for e in style_checks(good, s, CFG, ev, set())[0])


def test_broad_focus_no_longer_makes_every_result_unrelated():
    report = {'url': 'https://hacken.io/insights/q3-2026-report', 'title': 'Q3 2026 report',
              'content': 'Smart contract vulnerability losses across Ethereum and Solana rose this quarter.'}
    assert relevant_result(report, 'web3-security', 'Web3 audit coverage')
    vendor = {'url': 'https://hacken.io/our-team', 'title': 'Our team', 'content': 'We are auditors and engineers.'}
    assert not relevant_result(vendor, 'web3-security', 'Web3 audit coverage')


def test_body_must_really_contain_all_words_of_a_short_focus():
    snyk = 'Application security testing with pentest tools and owasp guidance. ' * 5       # no "supply chain"
    assert not body_relevant(snyk, 'pentesting', 'Supply chain risk')
    assert body_relevant(snyk + ' A supply chain attack hit a dependency. ' * 2, 'pentesting', 'Supply chain risk')


def test_ordinary_prevent_verbs_are_not_rejected_only_the_trailing_benefit_clause():
    from sanad.config import Slot
    ev = {'kb:SS-1:recommendation': 'Restrict instruction types per account type.'}
    s = Slot(id='t', type='tech-thread', focus='x', source_mode='kb')
    fine = Draft(posts=[Post(text='Restrict instruction types per account type to prevent unwanted operations.',
                             evidence_ids=['kb:SS-1:recommendation'])])
    assert not any('adds an effect' in e for e in style_checks(fine, s, CFG, ev, set())[0])
    bad = Draft(posts=[Post(text='Restrict instruction types per account type, preventing unwanted operations.',
                            evidence_ids=['kb:SS-1:recommendation'])])
    assert any('adds an effect' in e for e in style_checks(bad, s, CFG, ev, set())[0])


def test_judge_sees_the_same_excerpt_as_the_writer_and_title_citations_are_repaired(tmp_path):
    from sanad.agents import Pipeline
    from sanad.providers import DemoProvider
    from sanad.research import WebResearch, fixture_data
    from sanad.config import Draft as D, Post as P
    seen = {}

    class Spy(DemoProvider):
        async def generate(self, role, prompt, payload, schema, slot):
            if role == 'writer':
                seen['writer'] = payload['evidence']
                kb = [k for k in payload['evidence'] if k.endswith(':description')][0].rsplit(':', 1)[0]
                return D(posts=[P(text=['The first instruction accepts accounts without checks.','Permission checks only look at balance changes.','A limited role can still change authority.','The issue comes from unconstrained operations.','Restrict instruction types for each account type.'][i], evidence_ids=[kb + ':title']) for i in range(5)])
            if role == 'reviewer':
                seen['reviewer'] = payload['evidence']
            return await super().generate(role, prompt, payload, schema, slot)

    cfg = Config(ROOT)
    pipe = Pipeline(cfg, Spy(), WebResearch(cfg, 'demo', fixture=fixture_data()), tmp_path)
    slot_ = slot(id='t', type='tech-thread', focus='Access Control', source_mode='kb', service='', format='')
    state = asyncio.run(pipe.research({'slot': slot_.model_dump()}))
    state = {'slot': slot_.model_dump(), **state}
    state.update(asyncio.run(pipe.write(state)))
    assert all(r.endswith(':description') for p in state['draft']['posts'] for r in p['evidence_ids'])   # title -> description
    state.update(asyncio.run(pipe.review(state)))
    assert all(seen['reviewer'][k] == seen['writer'][k] for k in seen['reviewer'])                      # identical excerpts


# ---------------- run 8: all three marketing slots failed with an opaque "Live search failed"
class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body or {'results': []}

    def raise_for_status(self):
        import httpx
        if self.status_code >= 400:
            raise httpx.HTTPStatusError('x', request=httpx.Request('POST', 'https://api.tavily.com/search'),
                                        response=httpx.Response(self.status_code))

    def json(self):
        return self._body


class _Client:
    def __init__(self, codes):
        self.codes, self.calls = list(codes), 0

    async def post(self, url, json=None, timeout=None):
        self.calls += 1
        return _Resp(self.codes.pop(0) if len(self.codes) > 1 else self.codes[0])


def _search(client, monkeypatch):
    from sanad.research import SourceError, WebResearch
    async def fast(_):          # no real waiting in tests
        return None
    monkeypatch.setattr('sanad.research.asyncio.sleep', fast)
    wr = WebResearch(CFG, 'live', api_key='k', client=client)
    log = []
    try:
        asyncio.run(wr.search('pentesting', 'Supply chain risk', log, 'differentiation'))
    except SourceError as exc:
        return str(exc), log
    return None, log


@pytest.mark.parametrize('code,expect', [(401, 'rejected the API key'), (432, 'plan or credit limit'), (400, 'rejected the request')])
def test_search_failure_names_the_real_reason(code, expect, monkeypatch):
    message, log = _search(_Client([code]), monkeypatch)
    assert expect in message and 'HTTP %d' % code in message
    assert log[0]['status'].startswith('search_error:') and log[0]['search_error_detail']


def test_rate_limit_and_server_errors_are_retried_then_succeed(monkeypatch):
    client = _Client([429, 503, 200])
    _search(client, monkeypatch)
    assert client.calls >= 3              # the first query needed 3 attempts; later queries then return empty results
    assert client.codes[0] == 200


# ---------------- run 9: one unsupported sentence used to drop a whole piece
from sanad.config import Slot as _Slot
from sanad.quality import prune_unsupported


def _judged(draft, bad=(), scores=(4, 4, 4, 4)):
    claims = []
    for i, post in enumerate(draft.posts):
        for j, sent in enumerate(__import__('re').split(r'(?<=[.!?;])\s+', post.text)):
            uid = f'p{i + 1}s{j + 1}'
            claims.append(Claim(unit_id=uid, supported=uid not in bad, evidence_id=post.evidence_ids[0],
                                quote=sent if uid not in bad else '', reason='no' if uid in bad else 'ok'))
    a, u, c, b = scores
    return Judgment(approve=not bad, scores=Scores(accuracy=a, usefulness=u, clarity=c, brand_fit=b), claims=claims,
                    reasons=['x'] if bad else [])


def _thread(n=6):
    texts = [f'Sentence number {i} says something distinct about topic {i}.' for i in range(1, n + 1)]
    ev = {'kb:SS-1:description': ' '.join(texts)}
    return Draft(posts=[Post(text=t, evidence_ids=['kb:SS-1:description']) for t in texts]), ev


def test_unsupported_middle_post_is_removed_but_requires_fresh_review():
    draft, ev = _thread(6)
    spec = CFG.post_type(_Slot(id='t', type='tech-thread', focus='x', source_mode='kb'))
    result = prune_unsupported(draft, _judged(draft, bad={'p3s1'}), ev, CFG.style, spec, 'technical')
    new_draft, new_judgment, removed = result
    assert len(new_draft.posts) == 5 and removed['posts'] == [3]
    assert [c.unit_id for c in new_judgment.claims] == [f'p{i}s1' for i in range(1, 6)]    # renumbered
    assert not evaluate(new_judgment, new_draft, ev, CFG.style)['accepted']


@pytest.mark.parametrize('bad,n', [({'p1s1'}, 6), ({'p6s1'}, 6), ({'p3s1'}, 5), ({'p2s1', 'p3s1'}, 6)])
def test_pruning_refuses_to_remove_the_hook_the_mitigation_or_go_below_the_minimum(bad, n):
    draft, ev = _thread(n)
    spec = CFG.post_type(_Slot(id='t', type='tech-thread', focus='x', source_mode='kb'))
    assert prune_unsupported(draft, _judged(draft, bad=bad), ev, CFG.style, spec, 'technical') is None


def test_pruning_refuses_when_unsupported_claims_are_not_the_only_problem():
    draft, ev = _thread(6)
    spec = CFG.post_type(_Slot(id='t', type='tech-thread', focus='x', source_mode='kb'))
    assert prune_unsupported(draft, _judged(draft, bad={'p3s1'}, scores=(4, 4, 2, 4)), ev, CFG.style, spec, 'technical') is None


def test_marketing_post_loses_only_the_unsupported_sentence():
    text = 'Fact one is stated plainly. Fact two is invented here. Fact three is stated plainly. https://example.com/a'
    draft = Draft(posts=[Post(text=text, evidence_ids=['web:pentesting:1:body'])])
    ev = {'web:pentesting:1:body': 'Fact one is stated plainly. Fact three is stated plainly.'}
    spec = CFG.post_type(_Slot(id='m', type='marketing-post', focus='x', source_mode='web', service='pentesting', format='short-tip'))
    new_draft, new_judgment, removed = prune_unsupported(draft, _judged(draft, bad={'p1s2'}), ev, CFG.style, spec, 'marketing')
    assert new_draft.posts[0].text == 'Fact one is stated plainly. Fact three is stated plainly. https://example.com/a'
    assert removed['units'] == ['p1s2']
    two = Draft(posts=[Post(text='Fact one is stated plainly. Fact two is invented here.', evidence_ids=['web:pentesting:1:body'])])
    assert prune_unsupported(two, _judged(two, bad={'p1s2'}), ev, CFG.style, spec, 'marketing') is None   # would leave one sentence


@pytest.mark.parametrize('approve_edited', [True, False])
def test_pruned_thread_requires_independent_approval(tmp_path, approve_edited):
    import re as _re
    from sanad.agents import Pipeline
    from sanad.providers import DemoProvider
    from sanad.research import WebResearch, fixture_data

    class OneBadSentence(DemoProvider):
        reviews = 0
        async def generate(self, role, prompt, payload, schema, slot):
            if role == 'writer':
                ev = {k: v for k, v in payload['evidence'].items() if k.endswith((':description', ':recommendation'))}
                sents = []
                for key, text in ev.items():
                    for s in _re.split(r'(?<=[.!?])\s+', text):
                        if 40 <= len(s) <= 200 and s.isascii() and s not in [x[1] for x in sents]:
                            sents.append((key, s))
                return Draft(posts=[Post(text=s, evidence_ids=[k]) for k, s in sents[:6]])
            if role == 'reviewer':
                self.reviews += 1
                if self.reviews == 2 and approve_edited:
                    return await super().generate(role, prompt, payload, schema, slot)
                claims = [Claim(unit_id=u['unit_id'], supported=u['unit_id'] != 'p3s1',
                                evidence_id=payload['draft']['posts'][u['index']]['evidence_ids'][0],
                                quote=u['text'], reason='bad' if u['unit_id'] == 'p3s1' else 'ok') for u in payload['units']]
                return Judgment(approve=False, scores=Scores(accuracy=3, usefulness=4, clarity=4, brand_fit=4), claims=claims, reasons=['p3'])
            return await super().generate(role, prompt, payload, schema, slot)

    cfg = Config(ROOT)
    pipe = Pipeline(cfg, OneBadSentence(), WebResearch(cfg, 'demo', fixture=fixture_data()), tmp_path)
    sl = _Slot(id='t', type='tech-thread', focus='Access Control', source_mode='kb')
    state = {'slot': sl.model_dump()}
    state.update(asyncio.run(pipe.research(state)))
    state.update(asyncio.run(pipe.write(state)))
    before = len(state['draft']['posts'])
    out = asyncio.run(pipe.review(state))
    assert pipe.provider.reviews == 2
    assert out['accepted'] == approve_edited
    if not approve_edited:
        assert 'draft' not in out
        return
    assert before >= 6 and out['accepted'], out['reasons']
    assert len(out['draft']['posts']) == before - 1
    assert out['reviews'][-1]['pruned']['removed']['posts'] == [3]


# ---------------- run 17: daily token limit, identical weekly output, no way to retry only what failed
def test_recent_findings_are_avoided_when_alternatives_exist():
    from sanad.research import candidate_kb
    every = [k['id'] for k in candidate_kb(CFG, 'Access Control')]
    assert len(every) > 1
    fresh = [k['id'] for k in candidate_kb(CFG, 'Access Control', avoid={every[0]})]
    assert every[0] not in fresh and fresh                       # a different finding than last week
    assert [k['id'] for k in candidate_kb(CFG, 'Access Control', avoid=set(every))] == every   # nothing else left: reuse
    assert candidate_kb(CFG, '', avoid={k['id'] for k in CFG.kb[:3]})[0]['id'] not in {k['id'] for k in CFG.kb[:3]}


def test_recent_finding_ids_come_from_previous_run_folders(tmp_path):
    from sanad.agents import recent_finding_ids
    run = tmp_path / 'old'
    run.mkdir()
    (run / 'resolved_plan.json').write_text(json.dumps({'slots': [
        {'id': 'a', 'resolution_status': 'shipped', 'source_ids': ['SS-1']},
        {'id': 'b', 'resolution_status': 'failed', 'source_ids': ['SS-2']}]}))
    assert recent_finding_ids(tmp_path) == {'SS-1'}


def test_retry_keeps_approved_pieces_and_reruns_only_the_rest(tmp_path):
    from sanad.agents import run_week
    from sanad.config import Plan as P
    from sanad.providers import DemoProvider
    from sanad.research import WebResearch, fixture_data
    cfg = Config(ROOT)
    plan = P.model_validate_json((ROOT / 'data' / 'plan.json').read_text())
    web = WebResearch(cfg, 'demo', fixture=fixture_data())
    first = DemoProvider()
    summary = asyncio.run(run_week(plan, cfg, first, web, tmp_path / 'one'))
    assert summary['pieces_shipped'] == 6 and first.calls
    second = DemoProvider()
    summary = asyncio.run(run_week(plan, cfg, second, web, tmp_path / 'two', reuse=tmp_path / 'one'))
    assert summary['pieces_shipped'] == 6 and len(summary['reused_pieces']) == 6
    assert second.calls == []                                       # nothing re-run, no tokens spent
    changed = P.model_validate(plan.model_dump())
    changed.slots[0].focus = 'Oracle Manipulation'                  # the user edited one slot: only that one runs again
    third = DemoProvider()
    summary = asyncio.run(run_week(changed, cfg, third, web, tmp_path / 'three', reuse=tmp_path / 'one'))
    assert len(summary['reused_pieces']) == 5 and {c['slot'] for c in third.calls} == {changed.slots[0].id}


def test_roles_can_use_their_own_model_and_reasoning_is_low_for_gpt_oss():
    from sanad.providers import LiveProvider
    provider = LiveProvider({'OPENAI_API_KEY': 'k', 'OPENAI_BASE_URL': 'https://api.groq.com/openai/v1',
                             'LLM_MODEL': 'openai/gpt-oss-120b', 'LLM_MODEL_RESEARCH': 'openai/gpt-oss-20b'})
    assert provider._model_for('research') == 'openai/gpt-oss-20b' and provider._model_for('writer') == 'openai/gpt-oss-120b'
    assert provider._llm_for('research').model_name == 'openai/gpt-oss-20b'
    assert provider._llm_for('writer') is provider._llm_for('reviewer')          # same model and effort: one client
    assert provider._llm_for('writer').reasoning_effort == 'low'


def test_long_provider_reset_is_explained_and_stops_further_calls():
    import time
    from sanad.providers import LiveProvider, ProviderFailure

    class Limited(Exception):
        status_code = 429
        response = type('R', (), {'headers': {'retry-after': '1650'}})()

    class Runnable:
        calls = 0
        async def ainvoke(self, messages):
            Runnable.calls += 1
            raise Limited()

    class FakeLLM:
        def with_structured_output(self, *a, **k):
            return Runnable()

    provider = LiveProvider({'OPENAI_API_KEY': 'k', 'LLM_MODEL': 'gpt-4.1-mini'})
    provider._llm_for = lambda role: FakeLLM()
    payload = {'post_type': {'name': 'tech-thread'}}
    with pytest.raises(ProviderFailure) as first:
        asyncio.run(provider.generate('writer', 'p', payload, Draft, 's1'))
    assert 'resets in about 28 min' in str(first.value) and 'Retry failed pieces' in str(first.value)
    before = Runnable.calls
    with pytest.raises(ProviderFailure) as second:                      # the next slot does not call the provider again
        asyncio.run(provider.generate('reviewer', 'p', payload, Draft, 's2'))
    assert Runnable.calls == before and 'resets in about' in str(second.value)
