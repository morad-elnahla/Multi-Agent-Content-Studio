"""Regressions directly based on the user's failed 20 Oct live run."""
import asyncio
from pathlib import Path

from sanad.agents import Pipeline
from sanad.config import Claim, Config, Draft, Judgment, Post, Scores, Slot
from sanad.providers import DemoProvider
from sanad.quality import evaluate, style_checks, units
from sanad.research import PlainHTML, WebResearch, fixture_data, relevant_result
from sanad.skills import verbatim_quote_choices


def test_quote_options_retain_literal_60s_not_an_incomplete_truncation():
    c=Config()
    source=c.kb_by_id['SS-03738']['content']['description']
    options=verbatim_quote_choices(source,'An older price within the 60-second window can be used.',count=4,max_chars=180)
    assert any('60s' in q for q in options)
    assert all(q in source for q in options)


def test_cta_is_brand_copy_not_factual_claim_and_still_requires_web_source():
    c=Config()
    ref='web:agentic-security:1:body'
    evidence={ref:'A prompt injection may influence how a software agent uses its tools.'}
    draft=Draft(posts=[Post(text='Prompt injection can influence an AI agent. DM to discuss a security review.',evidence_ids=[ref])])
    judged_units=units(draft,c.style['approved_ctas'])
    assert len(judged_units)==1
    assert judged_units[0]['text']=='Prompt injection can influence an AI agent.'
    errors,_=style_checks(draft,Slot(id='m',type='marketing-post',source_mode='web',service='agentic-security'),c,evidence,set())
    assert errors==[]
    judgment=Judgment(approve=True,scores=Scores(accuracy=4,usefulness=4,clarity=4,brand_fit=4),
                      claims=[Claim(unit_id='p1s1',supported=True,evidence_id=ref,
                                    quote='A prompt injection may influence how a software agent uses its tools.',
                                    reason='Supported paraphrase')],reasons=[])
    assert evaluate(judgment,draft,evidence,c.style)['accepted']
    brand_only=Draft(posts=[Post(text='We offer AI agent security.',evidence_ids=['brand:approved_service'])])
    errors,_=style_checks(brand_only,Slot(id='m',type='marketing-post',source_mode='web',service='agentic-security'),c,
                           {'brand:approved_service':'Agentic security covers AI systems'},set())
    assert any('fetched web source' in error for error in errors)


def test_unapproved_pseudo_cta_cannot_skip_factual_review():
    c=Config()
    draft=Draft(posts=[Post(text='Guaranteed risk elimination. DM to guarantee your safety.',evidence_ids=['web:a:body'])])
    assert len(units(draft,c.style['approved_ctas']))==2


def test_unrelated_github_sources_rejected_even_when_domain_allowed():
    assert not relevant_result({'url':'https://github.com/gmh5225/awesome-skills',
             'title':'AI Coding Agent Skills and tools'}, 'web3-security','Web3 audit coverage')
    assert relevant_result({'url':'https://github.com/example/solidity-security-audit',
             'title':'Solidity audit findings for contracts'}, 'web3-security','Web3 audit coverage')
    assert relevant_result({'url':'https://genai.owasp.org/new',
             'title':'Trust boundaries'}, 'agentic-security','prompt injection')


def test_html_extractor_excludes_navigation_and_keeps_article():
    p=PlainHTML()
    p.feed('<html><body><nav>Actions Code GitHub menu</nav><main><h1>Oracle price feed validation</h1>'
           '<p>Noncanonical feeds can accept historical updates.</p></main><footer>Policy</footer></body></html>')
    assert ' '.join(p.parts)=='Oracle price feed validation Noncanonical feeds can accept historical updates.'


def test_marketing_research_attaches_authorized_company_scope_only_after_web(tmp_path):
    cfg=Config()
    pipe=Pipeline(cfg,DemoProvider(),WebResearch(cfg,mode='demo',fixture=fixture_data()),tmp_path)
    slot=Slot(id='m',type='marketing-post',service='pentesting',source_mode='web',focus='Supply chain')
    state=asyncio.run(pipe.research({'slot':slot.model_dump()}))
    assert any(k.startswith('fixture:') for k in state['evidence'])
    assert state['evidence']['brand:approved_service'].startswith('Web2 pentesting')
    assert state['research']['mode']=='demo'


def test_rule_of_three_returns_exact_phrases_to_fix_not_only_vague_error():
    cfg=Config();ref='kb:x:description'
    text=['Review permissions, validate signature flow, and check owners.',
          'Use access controls, audit the call path, and verify signers.',
          'Require a signer before any state update.',
          'Keep an evidence trail.',
          'Document the fix. [SS-03699]']
    draft=Draft(posts=[Post(text=t,evidence_ids=[ref]) for t in text])
    err,_=style_checks(draft,Slot(id='t',type='tech-thread'),cfg,
                        {ref:'Source text verified for test.'},set())
    flagged=next(error for error in err if 'Repeated rule-of-three' in error)
    assert 'Review permissions' in flagged and 'Use access controls' in flagged


def test_safe_provider_error_code_does_not_log_full_server_body():
    from sanad.providers import safe_error_code
    class Error(Exception):
        body={'error':{'code':'json_validate_failed','message':'sensitive raw response with secrets'}}
    assert safe_error_code(Error())=='json_validate_failed'
    class Other(Exception):
        body={'error':{'code':'unknown_issue','message':'private'}}
    assert safe_error_code(Other()) is None
