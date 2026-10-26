"""Offline regression tests for the reorganized system; no paid API required."""
import asyncio
import csv
import json
from pathlib import Path
import pytest
from sanad.config import ROOT, Config, Slot, Plan, Draft, Post, Judgment, Scores, Claim
from sanad.research import (WebResearch, SourceError, host_allowed,
                            candidate_kb, entry_evidence, fixture_data)
from sanad.agents import run_week
from sanad.quality import style_checks, evaluate, units
from sanad.providers import DemoProvider, TokenGate

@pytest.fixture
def cfg():return Config(ROOT)


def test_uploaded_knowledge_used(cfg):
    assert len(cfg.kb)==20
    assert 'Swig' in cfg.kb[0]['protocol']
    assert any('sign_v1' in f['title'] for f in cfg.kb)
    assert len(cfg.services)==4
    assert 'agentic-payments' in cfg.services


def test_domains_not_suffix_bypassed(cfg):
    from sanad.research import service_domains
    domains=service_domains(cfg,'agentic-security')
    deny=cfg.sources['_deny']
    assert host_allowed('https://genai.owasp.org/page',domains,deny)
    assert not host_allowed('https://genai.owasp.org.evil.tld/',domains,deny)
    assert not host_allowed('http://genai.owasp.org/page',domains,deny)
    assert not host_allowed('https://127.0.0.1/test',domains,deny)
    assert not host_allowed('https://localhost/test',domains,deny)
    assert not host_allowed('https://x.com/status/123', ['x.com'],deny)
    assert not host_allowed('https://genai.owasp.org:444/page',domains,deny)


def test_marketing_source_must_be_web():
    with pytest.raises(ValueError):Slot(id='m',type='marketing-post',service='agentic-security',source_mode='kb')
    with pytest.raises(ValueError):Slot(id='m',type='marketing-post',service='agentic-security',source_mode='external',source_url='https://example.org')
    with pytest.raises(ValueError):Slot(id='t',type='tech-thread',source_mode='web')
    with pytest.raises(ValueError):Slot(id='t',type='tech-thread',source_mode='external')
    with pytest.raises(ValueError):Plan(slots=[Slot(id='x',type='tech-thread'),Slot(id='x',type='tech-thread')])


def test_kb_retrieval_and_no_fallback(cfg):
    assert candidate_kb(cfg,'oracle')
    assert candidate_kb(cfg,'a-made-up-nonexistent-term')==[]
    example=candidate_kb(cfg,'oracle')[0]
    ev=entry_evidence(example)
    assert any(k.startswith('kb:') for k in ev)
    assert any('description' in k for k in ev)


def test_writing_rules_before_model(cfg):
    slot=Slot(id='x',type='marketing-post',service='web3-security',source_mode='web')
    ev={'web:1:body':'Real evidence text about security boundaries and tooling.'}
    forbidden=['In today\'s fast-paced world, security matters.',
               'Not just a test, but also a revolution.', 'A robust approach.',
               'Security — always.','Secure 💥 now!',
               'Source https://evil.example.com', 'Read evil.com for details.',
               'Read www.evil.com for details.',
               'We may generally typically see cases.']
    for txt in forbidden:
        errors,_=style_checks(Draft(posts=[Post(text=txt,evidence_ids=['web:1:body'])]),slot,cfg,ev,set())
        assert errors,(txt,errors)


def test_fetched_url_whitelist(cfg):
    slot=Slot(id='x',type='marketing-post',service='web3-security',source_mode='web',format='news-reaction')
    url='https://genai.owasp.org/article'
    ev={'web:1:body':'A grounded security incident needs clear response guidance.'}
    draft=Draft(posts=[Post(text=f'A grounded security incident needs clear response guidance. {url}',evidence_ids=['web:1:body'])])
    errs,_=style_checks(draft,slot,cfg,ev,set())
    assert any('not fetched' in e for e in errs)
    errs,_=style_checks(draft,slot,cfg,ev,{url})
    assert not errs


def test_reviewer_support_requires_quote(cfg):
    draft=Draft(posts=[Post(text='Security controls affect scope.',evidence_ids=['src'])])
    judgment=Judgment(approve=True,scores=Scores(accuracy=5,usefulness=5,clarity=5,brand_fit=5),
       claims=[Claim(unit_id='p1s1',supported=True,evidence_id='src',quote='imaginary quote here',reason='fine')],reasons=[])
    report=evaluate(judgment,draft,{'src':'Security controls affect scope.'},{'min_reviewer_total':16,'min_reviewer_each':3})
    assert not report['accepted'] and any('exact source' in r for r in report['reasons'])


def test_6_piece_demo_and_export(cfg,tmp_path):
    provider=DemoProvider();web=WebResearch(cfg,'demo',fixture=fixture_data())
    dest=tmp_path/'demo'
    summary=asyncio.run(run_week(cfg.plan,cfg,provider,web,dest,concurrency=2))
    assert summary['pieces_shipped']==6
    assert summary['pieces_not_shipped']==0
    assert summary['langgraph_used'] in (True,False)
    with (dest/'posts.csv').open(encoding='utf-8',newline='') as f:rows=list(csv.DictReader(f))
    assert len(rows)==6
    assert set(rows[0])=={'Post Text','Image'}
    assert all('illustration' in r['Image'].lower() and len(r['Image']) > 80 for r in rows)
    assert all((dest/'visuals'/f"{result['slot']['id']}.json").is_file() for result in json.loads((dest/'pieces.json').read_text()))
    pieces=json.loads((dest/'pieces.json').read_text())
    assert len(pieces)==6
    assert all(x['status']=='shipped' for x in pieces)
    assert all(x['visual']['image_generated']==False for x in pieces)
    marketing=[x for x in pieces if x['slot']['type']=='marketing-post']
    assert all(x['research']['mode']=='demo' for x in marketing)
    assert all(all(not k.startswith('kb:') for k in x['evidence']) for x in marketing)
    assert all(not x['allowed_urls'] for x in marketing)
    logs=json.loads((dest/'research-log.json').read_text())
    assert len(logs)==3
    assert all(x['status']=='fixture_not_live' and x['query'] for x in logs)


def test_pre_judge_errors_only_then_two_revisions(cfg,tmp_path):
    class Banned(DemoProvider):
        def __init__(self):super().__init__();self.writer_count=0
        async def generate(self,role,prompt,payload,schema,slot):
            result=await super().generate(role,prompt,payload,schema,slot)
            if role=='writer':
                self.writer_count+=1
                result.posts[0].text+=' #bad'
            return result
    p=Banned();w=WebResearch(cfg,'demo',fixture=fixture_data())
    plan=Plan(slots=[Slot(id='t',type='tech-thread',focus='Access Control')])
    dest=tmp_path/'rejected'
    summary=asyncio.run(run_week(plan,cfg,p,w,dest))
    assert summary['pieces_not_shipped']==1
    assert p.writer_count==3
    assert not any(call['role']=='reviewer' for call in p.calls)
    item=json.loads((dest/'pieces.json').read_text())[0]
    assert item['status']=='dropped'
    assert len(item['reviews'])==3
    assert all(not x['judge_called'] for x in item['reviews'])


def test_missing_search_key_fails_only_marketing(cfg,tmp_path):
    w=WebResearch(cfg,'live',api_key=None)
    plan=Plan(slots=[Slot(id='m',type='marketing-post',focus='recent security',service='web3-security',source_mode='web'),
                     Slot(id='t',type='tech-thread',focus='Access Control')])
    dest=tmp_path/'no-web'
    summary=asyncio.run(run_week(plan,cfg,DemoProvider(),w,dest))
    assert summary['pieces_shipped']==1
    assert summary['pieces_not_shipped']==1
    log=json.loads((dest/'research-log.json').read_text())
    assert log[0]['status']=='no_search_api' and log[0]['query']


def test_output_never_overwritten(cfg,tmp_path):
    out=tmp_path/'same'
    asyncio.run(run_week(Plan(slots=[Slot(id='x',type='tech-thread',focus='Access Control')]),cfg,
                          DemoProvider(),WebResearch(cfg,'demo',fixture=fixture_data()),out))
    with pytest.raises(FileExistsError):
        asyncio.run(run_week(Plan(slots=[Slot(id='x',type='tech-thread',focus='Access Control')]),cfg,
                              DemoProvider(),WebResearch(cfg,'demo',fixture=fixture_data()),out))


def test_budget_gate_concurrent_or_serial():
    async def attempt():
        token=TokenGate(budget=4000,window=.001)
        async with token.request(1000,'a'):
            token.record(1000)
        assert len(token.history)==1
        async with token.request(1000,'b'):
            pass
    asyncio.run(attempt())


def test_workflows_overlap_but_are_bounded(cfg,tmp_path):
    class Probe(DemoProvider):
        def __init__(self): super().__init__();self.active=0;self.peak=0
        async def generate(self,*args,**kwargs):
            self.active+=1;self.peak=max(self.peak,self.active)
            try:
                await asyncio.sleep(.01)
                return await super().generate(*args,**kwargs)
            finally:self.active-=1
    probe=Probe()
    small=Plan(slots=[Slot(id='a',type='tech-thread',focus='Access Control'),
                      Slot(id='b',type='tech-thread',focus='Oracle Manipulation'),
                      Slot(id='c',type='tech-thread',focus='Slippage Protection')])
    asyncio.run(run_week(small,cfg,probe,WebResearch(cfg,'demo',fixture=fixture_data()),tmp_path/'p',concurrency=2))
    assert probe.peak==2


def test_cross_service_queries_each_logged(cfg,tmp_path):
    one=Plan(slots=[Slot(id='cross',type='marketing-post',focus='agent payment policies',
                         service='agentic-security',services=['agentic-payments'],source_mode='web')])
    dest=tmp_path/'cross'
    result=asyncio.run(run_week(one,cfg,DemoProvider(),
                                WebResearch(cfg,'demo',fixture=fixture_data()),dest))
    assert result['pieces_shipped']==1
    logs=json.loads((dest/'research-log.json').read_text())
    assert {x['service'] for x in logs}=={'agentic-security','agentic-payments'}


def test_plan_rejects_repeated_marketing_format():
    with pytest.raises(ValueError):
        Plan(slots=[Slot(id=f'm{i}',type='marketing-post',focus=f'topic{i}',
                         service=service,source_mode='web',format='short-tip')
           for i,service in enumerate(('web3-security','pentesting','agentic-security'))])


def test_source_id_is_visible_in_technical_export(cfg,tmp_path):
    one=Plan(slots=[Slot(id='technical',type='tech-thread',focus='Access Control')])
    dest=tmp_path/'tech'
    summary=asyncio.run(run_week(one,cfg,DemoProvider(),WebResearch(cfg,'demo',fixture=fixture_data()),dest))
    assert summary['pieces_shipped']==1
    with (dest/'posts.csv').open(encoding='utf-8',newline='') as f:
        row=next(csv.DictReader(f))
    assert '[SS-' in row['Post Text']
    assert 'illustration' in row['Image'].lower()
    assert (dest/'visuals'/'technical.json').is_file()



def test_external_url_stays_offline_in_demo(cfg,tmp_path):
    p=Plan(slots=[Slot(id='external',type='tech-thread',focus='A new audit',
                       source_mode='external',source_url='https://docs.soliditylang.org/en/latest/security-considerations.html')])
    summary=asyncio.run(run_week(p,cfg,DemoProvider(),
                                WebResearch(cfg,'demo',fixture=fixture_data()),tmp_path/'external'))
    assert summary['pieces_not_shipped']==1
    piece=json.loads((tmp_path/'external'/'pieces.json').read_text())[0]
    assert 'strictly offline' in piece['reasons'][0]


def test_editable_markdown_prompts_are_loaded(cfg):
    from sanad.config import parse_prompts
    raw=(ROOT/'data'/'prompts.md').read_text(encoding='utf-8')
    prompts=parse_prompts(raw)
    assert set(prompts)=={'research','tech_writer','marketing_writer','reviewer','visual'}
    assert 'evidence' in prompts['reviewer'].lower()
    assert cfg.prompts==prompts
    with pytest.raises(ValueError,match='repeated'):
        parse_prompts(raw+'\n## research\nDuplicate content\n')
    with pytest.raises(ValueError,match='Missing'):
        parse_prompts('## research\nResearch only.')


def test_run_exports_machine_readable_schemas(cfg,tmp_path):
    plan=Plan(slots=[Slot(id='technical',type='tech-thread',focus='Access Control')])
    folder=tmp_path/'schemas'
    asyncio.run(run_week(plan,cfg,DemoProvider(),WebResearch(cfg,'demo',fixture=fixture_data()),folder))
    schemas=json.loads((folder/'schemas.json').read_text())
    assert set(schemas)=={'plan','draft','judgment','visual_choice'}
    assert 'properties' in schemas['judgment']
    pieces=json.loads((folder/'pieces.json').read_text())
    with (folder/'posts.csv').open(encoding='utf-8',newline='') as f:
        row=next(csv.DictReader(f))
    assert row['Image']==pieces[0]['visual']['prompt']
    assert pieces[0]['visual_path']=='visuals/technical.json'


def test_cli_can_change_marketing_service_without_editing_code(tmp_path,monkeypatch):
    import sys
    from run import main
    target=tmp_path/'service-override'
    monkeypatch.setattr(sys,'argv',['run.py','--mode','demo',
        '--set','marketing_1.service=agentic-payments',
        '--set','marketing_1=agent payment limits',
        '--output',str(target)])
    assert main()==0
    pieces=json.loads((target/'pieces.json').read_text())
    post=next(x for x in pieces if x['slot']['id']=='marketing_1')
    assert post['slot']['service']=='agentic-payments'
    assert post['slot']['focus']=='agent payment limits'
    assert post['research']['mode']=='demo'

# Release checks added after the original review: shared skill, schema, extensions and artifacts.
def test_shared_skill_used_by_research_and_review(monkeypatch, cfg, tmp_path):
    from sanad.skills import select_evidence
    from sanad import agents, quality
    assert agents.select_evidence is select_evidence
    assert quality.select_evidence is select_evidence
    assert select_evidence({'id':'text'},['id']) == {'id':'text'}
    with pytest.raises(ValueError): select_evidence({'id':'text'},['id','id'])
    with pytest.raises(ValueError): select_evidence({'id':'text'},['invented'])
    # Research must invoke shared resolver, and Reviewer must use it via code checks.
    counter={'research':0,'review':0}
    def research_resolver(*args,**kwargs):
        counter['research']+=1; return select_evidence(*args,**kwargs)
    def review_resolver(*args,**kwargs):
        counter['review']+=1; return select_evidence(*args,**kwargs)
    monkeypatch.setattr(agents,'select_evidence',research_resolver)
    monkeypatch.setattr(quality,'select_evidence',review_resolver)
    plan=Plan(slots=[Slot(id='t',type='tech-thread',focus='Access Control')])
    summary=asyncio.run(run_week(plan,cfg,DemoProvider(),
        WebResearch(cfg,'demo',fixture=fixture_data()),tmp_path/'shared'))
    assert summary['pieces_shipped']==1
    assert counter['research']>0 and counter['review']>0


def test_root_causes_all_grounded_without_changing_uploaded_kb(cfg):
    original=json.loads((ROOT/'data/kb.json').read_text(encoding='utf-8'))
    assert len(cfg.normalized_kb)==len(original['findings'])==20
    for raw, row in zip(original['findings'],cfg.normalized_kb):
        assert row['id']==raw['id']
        assert row['severity']==raw['impact']
        assert row['root_cause']
        assert row['root_cause_evidence_quote'] in raw['content']['description']
        assert row['description']==raw['content']['description']


def test_data_driven_new_technical_post_type_without_python_changes(cfg,tmp_path):
    from shutil import copytree
    root=tmp_path/'custom-project';copytree(ROOT/'data',root/'data')
    catalog=json.loads((root/'data/post-types.json').read_text())
    catalog['audit-lesson']={'source_policy':'technical','writer_prompt':'tech_writer',
                             'min_posts':3,'max_posts':7,'brand_rules':True}
    (root/'data/post-types.json').write_text(json.dumps(catalog))
    new_config=Config(root)
    slot=Slot(id='learning',type='audit-lesson',focus='Access Control',source_mode='kb')
    new_config.validate_plan(Plan(slots=[slot]))
    result=asyncio.run(run_week(Plan(slots=[slot]),new_config,DemoProvider(),
             WebResearch(new_config,'demo',fixture=fixture_data()),tmp_path/'custom-run'))
    assert result['pieces_shipped']==1
    piece=json.loads((tmp_path/'custom-run/pieces.json').read_text())[0]
    assert piece['slot']['type']=='audit-lesson'
    assert len(piece['draft']['posts'])==5


def test_new_marketing_type_preserves_web_only_security(cfg,tmp_path):
    from shutil import copytree
    root=tmp_path/'custom-marketing';copytree(ROOT/'data',root/'data')
    catalog=json.loads((root/'data/post-types.json').read_text())
    catalog['security-update']={'source_policy':'marketing','writer_prompt':'marketing_writer',
                                 'min_posts':1,'max_posts':1}
    (root/'data/post-types.json').write_text(json.dumps(catalog))
    c=Config(root)
    slot=Slot(id='m',type='security-update',service='agentic-security',source_mode='web')
    c.validate_plan(Plan(slots=[slot]))
    with pytest.raises(ValueError,match='web-only'):
        c.validate_plan(Plan(slots=[Slot(id='m',type='security-update',
                service='agentic-security',source_mode='kb')]))
    result=asyncio.run(run_week(Plan(slots=[slot]),c,DemoProvider(),
        WebResearch(c,'demo',fixture=fixture_data()),tmp_path/'marketing-run'))
    assert result['pieces_shipped']==1
    piece=json.loads((tmp_path/'marketing-run/pieces.json').read_text())[0]
    assert piece['research']['mode']=='demo'
    assert all(not key.startswith('kb:') for key in piece['evidence'])


def test_full_named_run_artifacts_and_failure_reasons(cfg,tmp_path):
    class AlwaysBad(DemoProvider):
        async def generate(self,role,prompt,payload,schema,slot):
            draft=await super().generate(role,prompt,payload,schema,slot)
            if role=='writer': draft.posts[0].text+=' #bad'
            return draft
    output=tmp_path/'review-audit'
    plan=Plan(slots=[Slot(id='fail',type='tech-thread',focus='Access Control')])
    summary=asyncio.run(run_week(plan,cfg,AlwaysBad(),
        WebResearch(cfg,'demo',fixture=fixture_data()),output))
    assert summary['pieces_dropped']==1
    assert summary['failures'][0]['slot_id']=='fail'
    for name in ('resolved_plan.json','research_log.json','drafts.json','review_report.json',
                 'final_content.json','run_summary.json','kb_normalized.json','posts.csv'):
        assert (output/name).is_file(),name
    assert len(json.loads((output/'drafts.json').read_text())['pieces'][0]['attempts'])==3
    reviews=json.loads((output/'review_report.json').read_text())['pieces'][0]['reviews']
    assert len(reviews)==3
    assert json.loads((output/'final_content.json').read_text())['approved']==[]
    assert json.loads((output/'run_summary.json').read_text())['failures'][0]['reasons']
    assert json.loads((output/'resolved_plan.json').read_text())['slots'][0]['source_ids']
    kb_out=json.loads((output/'kb_normalized.json').read_text())['findings']
    assert len(kb_out)==20 and all(v['root_cause'] for v in kb_out)


def test_undefined_content_type_and_wrong_policy_fail_closed(cfg,tmp_path):
    with pytest.raises(ValueError,match='Undefined content type'):
        cfg.validate_plan(Plan(slots=[Slot(id='unknown',type='unknown-kind')]))
    with pytest.raises(ValueError):
        cfg.validate_plan(Plan(slots=[Slot(id='x',type='marketing-post',
                       source_mode='web',service='unapproved-category')]))


def test_brand_new_writer_prompt_is_loaded_from_markdown_not_python(tmp_path):
    from shutil import copytree
    root=tmp_path/'type-with-new-writer';copytree(ROOT/'data',root/'data')
    catalog=json.loads((root/'data/post-types.json').read_text())
    catalog['audit-note']={'source_policy':'technical','writer_prompt':'audit_writer',
                           'min_posts':3,'max_posts':7}
    (root/'data/post-types.json').write_text(json.dumps(catalog))
    with (root/'data/prompts.md').open('a',encoding='utf-8') as f:
        f.write('\n## audit_writer\nExplain the security issue directly and cite source evidence.\n')
    c=Config(root)
    assert 'audit_writer' in c.prompts
    class PromptProbe(DemoProvider):
        def __init__(self):super().__init__();self.prompt_used=False
        async def generate(self, role, prompt, payload, schema, slot):
            if role=='writer':self.prompt_used=('Explain the security issue' in prompt)
            return await super().generate(role,prompt,payload,schema,slot)
    provider=PromptProbe()
    plan=Plan(slots=[Slot(id='lesson',type='audit-note',focus='Access Control')])
    summary=asyncio.run(run_week(plan,c,provider,WebResearch(c,'demo',fixture=fixture_data()),
                                 tmp_path/'fresh-prompt-run'))
    assert summary['pieces_shipped']==1 and provider.prompt_used


def test_fake_root_cause_refused_before_start(tmp_path):
    from shutil import copytree
    root=tmp_path/'bad-provenance';copytree(ROOT/'data',root/'data')
    causes=json.loads((root/'data/root-causes.json').read_text())
    causes['SS-03699']['evidence_quote']='fabricated quote not in the original'
    (root/'data/root-causes.json').write_text(json.dumps(causes))
    with pytest.raises(ValueError,match='literal substring'):
        Config(root)


def test_live_mode_cannot_silently_fall_back_to_demo_graph(cfg,tmp_path,monkeypatch):
    from sanad import agents
    monkeypatch.setattr(agents,'HAS_LANGGRAPH',False)
    class FakeLive(DemoProvider):mode='live'
    dest=tmp_path/'must-not-start'
    with pytest.raises(RuntimeError,match='LangGraph is required'):
        asyncio.run(run_week(Plan(slots=[Slot(id='x',type='tech-thread',focus='Access Control')]),
             cfg,FakeLive(),WebResearch(cfg,'demo',fixture=fixture_data()),dest))
    assert not dest.exists()
