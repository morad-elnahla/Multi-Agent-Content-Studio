"""Regressions observed in the 2026-10-20 live run. No network/API required."""
import asyncio
from pathlib import Path

from sanad.agents import Pipeline
from sanad.config import Config, Slot, Draft, Post
from sanad.providers import DemoProvider, ProviderFailure
from sanad.quality import style_checks, units
from sanad.research import WebResearch, fixture_data
from sanad.skills import canonical_evidence_ids, clean_typography, source_snippet, verbatim_quote_choices


def test_real_kb_short_ids_become_existing_field_ids_not_fake_sources():
    cfg=Config()
    finding=cfg.kb_by_id['SS-03699']
    from sanad.skills import entry_evidence
    evidence=entry_evidence(finding)
    for bare in ['SS-03699','kb:SS-03699']:
        ids,fix=canonical_evidence_ids([bare],evidence,'The TokenLimit role does not restrict changing token account authority.')
        assert len(ids)==1
        assert ids[0].startswith('kb:SS-03699:') and ids[0] in evidence
        assert fix and fix[0]['original']==bare
    bad,_=canonical_evidence_ids(['SS-NOT-A-FINDING'],evidence,'text')
    assert bad==['SS-NOT-A-FINDING']  # Unknown references remain rejected.


def test_web_shorthand_resolves_only_fetched_body():
    evidence={'web:pentesting:1:body':'Supply chain controls should include build dependencies.'}
    refs,fix=canonical_evidence_ids(['web:pentesting:1'],evidence,'Supply chain controls should include build dependencies.')
    assert refs==['web:pentesting:1:body'] and fix
    refs,_=canonical_evidence_ids(['web:pentesting:99'],evidence,'nothing')
    assert refs==['web:pentesting:99']


def test_typographic_punctuation_is_fixed_but_emoji_not_hidden():
    assert clean_typography('risk—failure in owner’s check')=="risk-failure in owner's check"
    assert '💥' in clean_typography('unsafe 💥 message')


def test_bare_citation_is_not_fake_factual_sentence():
    draft=Draft(posts=[Post(text='The protocol accepts historical feed prices. [SS-03738]',evidence_ids=['k'])])
    items=units(draft)
    assert len(items)==1
    assert 'historical' in items[0]['text']
    assert '[SS-' not in items[0]['text']
    draft=Draft(posts=[Post(text='Security checks should inspect the build process. https://example.org/report',evidence_ids=['k'])])
    assert len(units(draft))==1


def test_long_web_page_shrinks_into_verbatim_source_excerpt():
    body='Unrelated introduction. '*350+'Supply chain risk affects CI/CD access and package provenance. '+'Unrelated footer. '*200
    snippet=source_snippet(body,'supply chain risk',1200)
    assert len(snippet)<=1200 and snippet in body
    assert 'Supply chain risk' in snippet
    quote=verbatim_quote_choices(body,'CI/CD access',count=2)
    assert quote and all(x in body for x in quote)


def test_write_normalizes_alias_in_actual_agent_and_shortens_payload(tmp_path):
    cfg=Config()
    class Trace(DemoProvider):
        def __init__(self): super().__init__();self.payloads=[]
        async def generate(self,role,prompt,payload,schema,slot):
            if role=='writer':
                self.payloads.append(payload)
                return Draft(posts=[Post(text='The build process is part of the supply chain.', evidence_ids=['web:pentesting:1'])])
            return await super().generate(role,prompt,payload,schema,slot)
    provider=Trace()
    pipe=Pipeline(cfg,provider,WebResearch(cfg,'demo',fixture=fixture_data()),tmp_path)
    state={'slot':Slot(id='m',type='marketing-post',focus='Supply chain',source_mode='web',service='pentesting').model_dump(),
           'topic':'Supply chain', 'evidence':{'web:pentesting:1:body':'Security teams inspect CI/CD processes. '+ 'A'*10000},
           'allowed_urls':[],'research':{'source_ids':['web:pentesting:1'],'provenance':{}}}
    result=asyncio.run(pipe.write(state))
    assert result['draft']['posts'][0]['evidence_ids']==['web:pentesting:1:body']
    assert len(provider.payloads[0]['evidence']['web:pentesting:1:body'])<=1400
    assert provider.payloads[0]['allowed_evidence_ids']==['web:pentesting:1:body']
    errs,_=style_checks(Draft.model_validate(result['draft']),Slot.model_validate(state['slot']),cfg,state['evidence'],set())
    assert errs==[]
    assert result['drafts'][0]['safe_repairs']


def test_provider_failure_keeps_safe_status_in_slot_reason(tmp_path):
    cfg=Config();pipe=Pipeline(cfg,DemoProvider(),WebResearch(cfg,'demo',fixture=fixture_data()),tmp_path)
    async def fail(_):
        raise ProviderFailure('writer provider failure: HTTP 413 request too large; reduce source excerpts')
    state={'slot':Slot(id='m',type='marketing-post',source_mode='web',service='pentesting').model_dump()}
    result=asyncio.run(pipe.wrap('write',fail)(state))
    assert result['status']=='failed'
    assert 'HTTP 413' in result['reasons'][0]


def test_judge_quote_format_retry_does_not_skip_semantic_checks(tmp_path):
    from sanad.config import Judgment, Scores, Claim
    cfg=Config()
    class QuoteFailOnce(DemoProvider):
        def __init__(self): super().__init__();self.reviews=0
        async def generate(self,role,prompt,payload,schema,slot):
            if role=='reviewer':
                self.reviews+=1
                record=payload['units'][0]
                quote=payload['exact_quote_options'][record['unit_id']][0]
                return Judgment(approve=True,
                    scores=Scores(accuracy=4,usefulness=4,clarity=4,brand_fit=4),
                    claims=[Claim(unit_id=record['unit_id'],supported=True,
                            evidence_id=quote['evidence_id'],
                            quote='Not a real source substring' if self.reviews==1 else quote['quote'],
                            reason='Supported by cited phrase')],reasons=[])
            return await super().generate(role,prompt,payload,schema,slot)
    provider=QuoteFailOnce()
    pipe=Pipeline(cfg,provider,WebResearch(cfg,'demo',fixture=fixture_data()),tmp_path)
    evidence={'web:pentesting:1:body':'A software supply chain depends on the build process.'}
    state={'slot':Slot(id='m',type='marketing-post',focus='Supply chain',source_mode='web',
                       service='pentesting').model_dump(),
           'draft':Draft(posts=[Post(text='Software supply chains depend on the build process.',
                         evidence_ids=['web:pentesting:1:body'])]).model_dump(),
           'evidence':evidence,'topic':'Supply chain',
           'allowed_urls':[],'attempts':1,
           'research':{'provenance':{'web:pentesting:1:body':'https://owasp.org/example'}},
           'reviews':[]}
    result=asyncio.run(pipe.review(state))
    assert result['accepted']
    assert result['reviews'][0]['judge_quote_retry'] is True
    assert provider.reviews==2
