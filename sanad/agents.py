"""Whole content workflow. One LangGraph per post; bounded concurrent post runs."""
from __future__ import annotations
import asyncio
import csv
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from .config import Config, Plan, Slot, Draft, Pick, Judgment, VisualIdea
from .providers import DemoProvider, ProviderFailure
from .delivery import write_delivery_html
from .quality import X_URL_WEIGHT, style_checks, evaluate, prune_unsupported, units, week_checks
from .research import WebResearch, SourceError, candidate_kb, fetch_url, fixture_data
from .skills import (entry_evidence, select_evidence, canonical_evidence_ids,
                     clean_typography, strip_inline_evidence_markers,
                     source_snippet, verbatim_quote_choices)

try:
    from langgraph.graph import StateGraph, START, END
    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False  # Core can be demo-tested where extra UI/graph deps are absent.


def utcnow(): return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,indent=2,ensure_ascii=False,default=str)+'\n',encoding='utf-8')
    os.replace(temp,path)


class State(TypedDict,total=False):
    slot: dict
    topic: str
    research: dict
    evidence: dict
    allowed_urls: list
    draft: dict
    attempts: int
    drafts: list
    reviews: list
    status: str
    accepted: bool
    reasons: list
    visual: dict
    writer_evidence: dict


class Pipeline:
    def __init__(self, config:Config, provider, web:WebResearch, output:Path, on_event=None, recent_ids=None):
        self.config=config;self.provider=provider;self.web=web
        self.recent_ids=set(recent_ids or ())
        self.output=Path(output);self.events=[];self.query_log=[]
        self.on_event=on_event or (lambda event:None)
        self.reserved=set()
        self.reservation_lock=asyncio.Lock()
        self.marketing_topics=[]
        if HAS_LANGGRAPH:
            g=StateGraph(State)
            for name in ('research','write','review','visual'):
                g.add_node(name,self.wrap(name,getattr(self,name)))
            g.add_edge(START,'research')
            g.add_conditional_edges('research',lambda s:END if s.get('status')=='failed' else 'write')
            g.add_conditional_edges('write',lambda s:END if s.get('status')=='failed' else 'review')
            g.add_conditional_edges('review',lambda s:END if s.get('status') in ('dropped','failed') else
                                    ('visual' if s.get('accepted') else 'write'))
            g.add_edge('visual',END)
            self.graph=g.compile()

    def wrap(self,name,fn):
        async def wrapped(state):
            self.event(state['slot']['id'],name,'start')
            try:
                auth_error=getattr(self.provider,'authentication_error','')
                if auth_error and name!='visual':
                    raise ProviderFailure(auth_error)
                # Do not spend search credits or start new work after the shared
                # provider has already reported an exhausted long-window quota.
                blocked=getattr(self.provider,'blocked_until',0)
                if blocked>time.monotonic() and name!='visual':
                    minutes=max(1,round((blocked-time.monotonic())/60))
                    raise ProviderFailure(f'Provider quota unavailable for about {minutes} min; saved work can be resumed with Retry failed pieces')
                update=await fn(state)
            except Exception as exc:
                # No raw API response body, search result or secret in run logs.
                detail=str(exc) if isinstance(exc,(SourceError,ProviderFailure)) else type(exc).__name__
                update={'status':'failed','reasons':[f'{name}: {detail}']}
            final=update.get('status') in ('dropped','failed')
            self.event(state['slot']['id'],name,'end',status=update.get('status','running'),
                       **({'reasons':[str(r)[:220] for r in update.get('reasons',[])[:2]]} if final else {}))
            return update
        return wrapped

    def event(self,slot,stage,kind,**extra):
        item={'slot':slot,'stage':stage,'event':kind,'time':utcnow(),**extra}
        self.events.append(item)
        self.on_event(dict(item))

    async def research(self,state):
        slot=Slot.model_validate(state['slot'])
        policy=self.config.post_type(slot)['source_policy']
        if policy=='marketing':
            # Hard routing: no KB access, even if prompts attempt to change it.
            # Dedupe similar topics across concurrent marketing slots, as required.
            tokens=set(re.findall(r'[a-z0-9]+',slot.focus.lower()))
            async with self.reservation_lock:
                if tokens and any(len(tokens & prev)/max(len(tokens|prev),1)>=.66 for prev in self.marketing_topics):
                    raise SourceError('Marketing topic too similar to another slot; change the focus')
                if tokens:self.marketing_topics.append(tokens)
            found=[]
            for service in dict.fromkeys([slot.service]+slot.services):
                found.extend(await self.web.search(service,slot.focus,self.query_log,slot.format))
            candidates=[{'id':x['id'],'title':x['title'],'source_url':x['url'],
                         'excerpt':x['body'][:1400]} for x in found]
            pick=await self.provider.generate('research',self.config.prompts['research'],
                    {'focus':slot.focus,'service':slot.service,'cross_services':slot.services,'candidates':candidates}, Pick,slot.id)
            chosen=[x for x in found if x['id'] in pick.source_ids]
            if not chosen or len(chosen)!=len(pick.source_ids):
                raise SourceError('Research selected invalid or duplicate candidate IDs')
            candidate_evidence={x['id']+':body':x['body'] for x in found}
            evidence=select_evidence(candidate_evidence,[x['id']+':body' for x in chosen])
            provenance={x['id']+':body':x['url'] for x in chosen}
            # Marketing guide is the authoritative source for SanadSquad's OWN
            # service scope; factual news still MUST use fetched web evidence.
            service_lines={
                'web3-security':'Web3 security | Smart contract audits across EVM and Solana / SVM, protocol design review, formal reasoning about invariants',
                'pentesting':'Web2 pentesting | The infrastructure around the protocol: APIs, cloud, CI/CD, dependencies, internal access, the frontend',
                'agentic-security':'Agentic security and red teaming | AI agent systems: prompt injection, tool abuse, unauthorized action, agent wallets and spending policy',
                'agentic-payments':'Agentic payment protocols | x402 and MPP: integration review, facilitator trust, payment policy design',
            }
            brand_line=service_lines.get(slot.service,'')
            if brand_line and brand_line.split(' | ',1)[1] in self.config.marketing:
                evidence['brand:approved_service']=brand_line
                provenance['brand:approved_service']='company marketing-rules.md (approved positioning)'
            position=[x for x in ('Most Web3 security firms audit smart contracts and stop there.',
                                  'SanadSquad covers the whole attack surface, not just the contract.')
                      if x in self.config.marketing]
            if position:
                evidence['brand:positioning']=' '.join(position)
                provenance['brand:positioning']='company marketing-rules.md (approved positioning, company opinion)'
            # Explicit vulnerability-class exception (separate from the WEB research agent).
            # It cannot replace a missing web result and must retain its KB ID citation.
            if slot.vulnerability_kb_id:
                finding=self.config.kb_by_id.get(slot.vulnerability_kb_id)
                if finding is None:
                    raise SourceError('Unknown optional vulnerability KB finding')
                topic=slot.focus.lower()
                if not (finding['title'].lower() in topic or any(t.lower() in topic for t in finding.get('tags',[]) if len(t)>3)):
                    raise SourceError('Optional KB citation must be a vulnerability class')
                specific=entry_evidence(finding)
                evidence.update({k:v for k,v in specific.items() if v})
                provenance.update({k:finding['source'].get('report_name','') for k,v in specific.items() if v})
            urls=[x['url'] for x in chosen if x['fetched']]
            return {'topic':slot.focus or slot.service,'research':{'mode':self.web.mode,
                    'source_ids':pick.source_ids,'reason':pick.reason,'provenance':provenance,
                    'sources':[{'id':x['id'],'title':x['title'],'url':x['url'],
                                'fetched':x['fetched']} for x in chosen]},
                    'evidence':evidence,'allowed_urls':urls,
                    'attempts':0,'drafts':[],'reviews':[],'status':'running'}
        if slot.source_mode=='external':
            if self.web.mode=='demo':
                raise SourceError('External technical URLs require Live mode; Demo is strictly offline')
            from urllib.parse import urlsplit
            host=urlsplit(slot.source_url).hostname or ''
            record={'slot':slot.id,'type':'technical_external','query':'',
                    'urls':[{'url':slot.source_url,'status':'requested'}]}
            self.query_log.append(record)
            try:
                body=await fetch_url(slot.source_url,[host],self.config.sources['_deny'])
                record['urls'][0]['status']='fetched'
            except Exception:
                record['urls'][0]['status']='fetch_failed'
                raise
            return {'topic':slot.focus or 'Technical resource','research':{
                     'mode':'external_live','source_ids':['ext:1'],'reason':'User-supplied external technical resource',
                     'provenance':{'ext:1:body':slot.source_url},
                     'sources':[{'id':'ext:1','url':slot.source_url,'fetched':True}]},
                    'evidence':select_evidence({'ext:1:body':body}, ['ext:1:body']), 'allowed_urls':[slot.source_url],
                    'attempts':0,'drafts':[],'reviews':[],'status':'running'}
        async with self.reservation_lock:
            candidates=candidate_kb(self.config,slot.focus,self.reserved,self.recent_ids)
            if not candidates:
                raise SourceError('No matching free KB finding; change topic or source mode')
            # Reserve before async model call so concurrent pieces cannot take same finding.
            chosen_pool=candidates[:3]
            self.reserved.update(x['id'] for x in chosen_pool)
        choices=[{'id':x['id'],'title':x['title'],'impact':x['impact'],
                  'tags':x.get('tags',[]),'protocol':x.get('protocol','')} for x in chosen_pool]
        pick=await self.provider.generate('research',self.config.prompts['research'],
                {'focus':slot.focus,'candidates':choices},Pick,slot.id)
        if not set(pick.source_ids).issubset({x['id'] for x in chosen_pool}) or len(pick.source_ids)!=len(set(pick.source_ids)):
            raise SourceError('Research chose sources outside its approved KB candidates')
        # A thread is about ONE finding. Mixing three unrelated findings made 8-post threads whose posts had to be
        # judged against different sources and were rejected for length and for "combined" claims.
        selected=[x for x in chosen_pool if x['id'] in pick.source_ids][:1]
        async with self.reservation_lock:
            self.reserved.difference_update(x['id'] for x in chosen_pool if x not in selected)
        pick.source_ids=[x['id'] for x in selected]
        raw_evidence={k:v for x in selected for k,v in entry_evidence(x).items() if v}
        for x in selected:
            key='kb:'+x['id']+':root_cause'
            raw_evidence[key]=self.config.kb_root_causes[x['id']]['root_cause']
        evidence=select_evidence(raw_evidence,list(raw_evidence))
        provenance={k:x['source'].get('report_name','')
                    for x in selected for k,v in entry_evidence(x).items() if v}
        provenance.update({'kb:'+x['id']+':root_cause':x['source'].get('report_name','') for x in selected})
        for finding in selected:
            context_id='kb:'+finding['id']+':context'
            evidence[context_id]='Reported finding metadata: '+json.dumps({
                'protocol':finding.get('protocol'),
                'reported_status':finding.get('content',{}).get('status'),
                'status_note':'Reported status only; no verified remediation date is supplied'},ensure_ascii=True)
            provenance[context_id]=finding['source'].get('report_name','')
        return {'topic':slot.focus or selected[0]['title'],
                'research':{'mode':'local_kb','source_ids':pick.source_ids,
                            'reason':pick.reason,'provenance':provenance,
                            'sources':[{'id':x['id'],'title':x['title'],'fetched':False,
                                        'report_url':x['source'].get('report_name','')} for x in selected]},
                'evidence':evidence,'allowed_urls':[],'attempts':0,
                'drafts':[],'reviews':[],'status':'running'}

    async def write(self,state):
        slot=Slot.model_validate(state['slot'])
        spec=self.config.post_type(slot)
        policy=spec['source_policy']
        prompt_key=spec['writer_prompt']
        instructions=self.config.prompts[prompt_key]+'\n\nBRAND VOICE:\n'+self.config.brand
        instructions+='\nPUBLICATION POLICY: Never include URLs or domain links in post text. Sources are displayed separately. The company name is exactly SanadSquad. This overrides any link requirements in source guides.'
        instructions+='\nINLINE CITATION POLICY: NEVER write source markers like 【kb:SS-03738:description】 or [web:...] inside post text. Only put the IDs in evidence_ids; the application handles visible references.'
        if policy=='marketing':
            instructions+='\n\nMARKETING RULES:\n'+self.config.marketing
        # Never pass a 10,000-character web page into every LLM call. These are
        # verbatim excerpts from the actual sources, not invented summaries.
        writer_evidence={k:source_snippet(v,state['topic'],1300) for k,v in state['evidence'].items()
                         if not (policy=='technical' and k.endswith((':impact',':protocol')))}
        # URLs remain in source metadata, never in publishable copy.
        required_link = ''
        suffix_len=(len(' ['+', '.join(state['research']['source_ids'])+']')
                    if policy=='technical' and slot.source_mode=='kb' else 0)
        remaining = max(1,self.config.style['max_characters']-suffix_len)
        feedback=list(dict.fromkeys(reason for review in state.get('reviews',[]) for reason in review['reasons']))
        overlength=any('exceeds 280 characters' in reason for reason in feedback)
        if policy=='marketing':
            instructions+='\nWrite at most two sentences: one specific sourced observation and one approved service connection. Omit sales CTAs. Use full words, not compressed abbreviations. Target 220 characters before any source link.'
        if overlength:
            instructions+='\nLENGTH REWRITE: Rebuild the post from its single most useful fact. Remove secondary facts and the CTA; do not shorten every sentence by a few characters. Never truncate a sentence or remove a necessary qualification. Target 210 characters before the citation. Return the complete Draft object.'
        # The style checker enforces a tighter limit for technical threads
        # than post-types.json; send the SAME effective maximum to the writer.
        effective_max = (min(spec['max_posts'], self.config.style.get('technical_preferred_max_posts', 7))
                         if policy == 'technical' else spec['max_posts'])
        if policy == 'technical':
            instructions += f'\nHARD LIMIT: Write between {spec["min_posts"]} and {effective_max} posts, including revisions. Never split content into extra posts to solve a review problem.'
        payload={'post_type':{'name':slot.type,'min_posts':spec['min_posts'],
                              'max_posts':effective_max,'source_policy':policy},
                 'topic':state['topic'],'focus':slot.focus,'format':slot.format,
                 'source_mode':slot.source_mode if policy=='technical' else 'web_only',
                 'evidence':writer_evidence, 'allowed_evidence_ids':list(writer_evidence),
                 'allowed_urls':[], 'required_source_link':required_link, 'links_in_post':False,
                 'max_last_post_chars_before_suffix':remaining,
                 'source_ids':state['research']['source_ids'],
                 'previous_draft':state.get('draft'),
                 'feedback':feedback,
                 'length_budget':{'maximum':remaining,'target':min(210 if overlength else 220,remaining),
                                  'rewrite_from_one_fact':overlength}}
        payload['finding_context']=[{'id':identifier,
            'protocol':self.config.kb_by_id[identifier].get('protocol'),
            'reported_status':self.config.kb_by_id[identifier].get('content',{}).get('status'),
            'status_note':'No verified remediation date is supplied'}
            for identifier in state['research']['source_ids'] if identifier in self.config.kb_by_id]
        if payload['finding_context']:
            instructions+='\nUse finding_context to identify the audited protocol and historical setting. Do not imply a reported finding is still exploitable today. Do not invent a fix date or claim a fix was independently verified. Cite the supplied evidence for factual mechanism claims.'
        draft=await self.provider.generate('writer',instructions,payload,Draft,slot.id)
        repairs=[]
        for i,post in enumerate(draft.posts):
            canonical,changes=canonical_evidence_ids(post.evidence_ids,state['evidence'],post.text)
            if changes:
                repairs.append({'post':i+1,'evidence_ids':changes})
            post.evidence_ids=canonical
            if policy=='technical':
                # Run 7: thread 3 was dropped twice because the opening post cited only the finding TITLE, which code
                # rightly refuses as proof of a claim. Move such a citation to the finding's description. This does not
                # weaken grounding: the judge must still quote the description for the sentence to be approved.
                meta=[r for r in post.evidence_ids if r.endswith((':title',':impact',':protocol'))]
                if meta:
                    keep=[r for r in post.evidence_ids if r not in meta]
                    fallback=meta[0].rsplit(':',1)[0]+':description'
                    if not keep and fallback in state['evidence']:
                        keep=[fallback]
                    if keep!=post.evidence_ids:
                        repairs.append({'post':i+1,'evidence_ids':'title/metadata citation replaced by description'})
                        post.evidence_ids=keep
            original_text=post.text
            clean=clean_typography(original_text)
            typography_changed=clean != original_text
            if policy=='technical':
                # Technical threads end with a finding or mitigation, not a
                # sales closing. Remove only standalone promotional sentences;
                # factual sentences remain untouched for the reviewer.
                sentences=re.split(r'(?<=[.!?])\s+',clean)
                promotional=re.compile(
                    r'(?i)\b(?:dm\s+to\s+discuss|consider\s+(?:a\s+)?security\s+review|'
                    r'request\s+(?:a\s+)?security\s+review|schedule\s+(?:a\s+)?security\s+review|'
                    r'review\s+your\b|want\s+(?:a\s+)?security\s+review)\b')
                compact=' '.join(s for s in sentences if not promotional.search(s)).strip()
                if compact != clean:
                    repairs.append({'post':i+1,'content':'removed promotional closing sentence'})
                    clean=compact
            # "e.g." / "i.e." split sentences in the middle (the judge then sees a fragment as a claim) and read as machine
            # text. They carry no facts, so write them out.
            spelled=re.sub(r'\bi\.e\.,?\s+','that is, ',re.sub(r'\be\.g\.,?\s+','for example, ',clean,flags=re.I),flags=re.I)
            if spelled!=clean:
                repairs.append({'post':i+1,'content':'spelled out e.g./i.e.'}); clean=spelled
            stripped=re.sub(r'\s*\[(?:web|brand|fixture|kb|ext):[^\]]*\]','',clean).strip()
            if stripped != clean:
                repairs.append({'post':i+1,'content':'removed evidence IDs written inside post text'})
                clean=stripped
            de_cited=strip_inline_evidence_markers(clean,state['evidence'])
            if de_cited != clean:
                repairs.append({'post':i+1,'content':'removed verified inline citation labels; evidence_ids retained'})
                clean=de_cited
            if typography_changed:
                repairs.append({'post':i+1,'typography':'normalized Unicode punctuation only'})
            if clean != original_text:
                post.text=clean
        before_count=len(draft.posts)
        draft.posts=[post for post in draft.posts if post.text.strip()]
        if len(draft.posts) != before_count:
            repairs.append({'content':'removed empty posts after promotional cleanup'})
        if repairs:
            self.event(slot.id,'write','normalized',changes=repairs)
        # Safe, deterministic citation: only IDs/URLs that research actually verified.
        # The final text INCLUDING this citation is checked by Python and the judge.
        if policy=='marketing' and slot.vulnerability_kb_id and draft.posts:
            visible=' ['+slot.vulnerability_kb_id+']'
            if visible.strip() not in ' '.join(p.text for p in draft.posts):
                draft.posts[-1].text+=visible
        if policy=='technical' and draft.posts:
            if slot.source_mode=='kb':
                citation=' ['+', '.join(state['research']['source_ids'])+']'
            else:
                citation=''
            if citation and citation.strip() not in ' '.join(p.text for p in draft.posts):
                draft.posts[-1].text+=citation
        n=state.get('attempts',0)+1
        return {'draft':draft.model_dump(),'attempts':n,'writer_evidence':writer_evidence,
                'drafts':state.get('drafts',[])+[{'attempt':n,'draft':draft.model_dump(),
                                                 'safe_repairs':repairs}]}

    async def review(self,state):
        slot=Slot.model_validate(state['slot']); draft=Draft.model_validate(state['draft'])
        # Shared Skill #2: Review resolves the same evidence IDs selected by Research.
        # Fail closed if a writer invents source IDs, before the model Judge.
        declared=[ref for post in draft.posts for ref in post.evidence_ids]
        try: select_evidence(state['evidence'], list(dict.fromkeys(declared)))
        except ValueError: pass  # quality.style_checks reports precise per-post errors
        errors,stats=style_checks(draft,slot,self.config,state['evidence'],set(state['allowed_urls']))
        accepted_draft=None
        report={'attempt':state['attempts'],'statistics':stats,'hard_errors':errors,
                'score':None,'accepted':False,'reasons':list(errors), 'judge_called':False}
        # Requested ordering: code rules must reject BEFORE any judge request.
        if not errors:
            items=units(draft,self.config.style.get('approved_ctas',[]))
            # Judge receives exact quote candidates drawn from original evidence.
            # They help formatting only: the LLM must still evaluate meaning.
            # The judge must see the SAME excerpt the writer saw. Different windows (1100 vs 1400 characters, chosen
            # around different positions) let the writer use a number that was invisible to the judge, so a true claim
            # about "~10k dev environments" was rejected as unsupported.
            seen_by_writer=state.get('writer_evidence') or {}
            quote_options={}
            for item in items:
                post=draft.posts[item['index']]
                quote_options[item['unit_id']]=[
                    {'evidence_id':ref,'quote':quote}
                    for ref in post.evidence_ids for quote in
                    verbatim_quote_choices(seen_by_writer.get(ref) or state['evidence'].get(ref,''),item['text'],count=6,max_chars=240)]
            used=set(ref for post in draft.posts for ref in post.evidence_ids)
            reviewer_evidence={k:(seen_by_writer.get(k) or source_snippet(state['evidence'][k],state['topic'],1300))
                               for k in used if k in state['evidence']}
            judge_payload={'draft':state['draft'],'evidence':reviewer_evidence,
                           'editorial_context':{'topic':state['topic'],'type':slot.type,'format':slot.format,
                               'requirements':['Each post adds a distinct idea; reject semantic repetition even with different words.',
                                   'Identify the subject and frame historical audit findings as historical.',
                                   'Reject unexplained conflicting time windows, vague incident references and unsupported claims of current trends.',
                                   'Assess the complete piece for coherence and publication quality, not only individual citation support.']},
                           'exact_quote_options':quote_options,
                           'units':items,'rubric':self.config.style,
                           'brand':self.config.brand[:800],
                           'source_provenance':{k:v for k,v in state['research']['provenance'].items() if k in used}}
            judgment=await self.provider.generate('reviewer',self.config.prompts['reviewer'],
                                                  judge_payload,Judgment,slot.id)
            verdict=evaluate(judgment,draft,state['evidence'],self.config.style)
            # One judge-only repair for exact-quote formatting failures; don't
            # waste a writer revision on a purely mechanical quote transcription.
            # Semantic rejections, bad evidence IDs and low grades are NOT retried.
            quote_only=bool(verdict['reasons']) and all(
                'quote is not an exact source substring' in reason for reason in verdict['reasons'])
            if quote_only:
                strict_prompt=(self.config.prompts['reviewer'] + '\n\nSECOND PASS: The previous '
                    'response failed ONLY the exact-quote transcription check. '
                    'Copy a quote VERBATIM from exact_quote_options; do not paraphrase. '
                    'Do not change unsupported facts into supported facts.')
                judge_payload['quote_retry_errors']=verdict['reasons']
                judgment=await self.provider.generate('reviewer',strict_prompt,
                                                      judge_payload,Judgment,slot.id)
                verdict=evaluate(judgment,draft,state['evidence'],self.config.style)
                report['judge_quote_retry']=True
            report.update(verdict);report['judge_called']=True
            if not verdict['accepted']:
                # Keep the supported part instead of dropping everything for one unsupported sentence or post.
                spec=self.config.post_type(slot)
                pruned=prune_unsupported(draft,judgment,state['evidence'],self.config.style,spec,spec['source_policy'])
                if pruned:
                    new_draft,new_judgment,removed=pruned
                    # Deletion changes coherence and usefulness. Never manufacture
                    # approval or raise accuracy based only on substring checks.
                    pruned_payload=dict(judge_payload, draft=new_draft.model_dump(),
                                        units=units(new_draft,self.config.style.get('approved_ctas',[])))
                    pruned_payload['exact_quote_options']={item['unit_id']:[
                        {'evidence_id':ref,'quote':quote}
                        for ref in new_draft.posts[item['index']].evidence_ids
                        for quote in verbatim_quote_choices(state['evidence'].get(ref,''),item['text'],count=6,max_chars=240)]
                        for item in pruned_payload['units']}
                    new_judgment=await self.provider.generate('reviewer',self.config.prompts['reviewer'],
                                                            pruned_payload,Judgment,slot.id)
                    report['pruned_review']=new_judgment.model_dump()
                    new_verdict=evaluate(new_judgment,new_draft,state['evidence'],self.config.style)
                    new_errors,new_stats=style_checks(new_draft,slot,self.config,state['evidence'],
                                                      set(state.get('allowed_urls',[])))
                    if new_verdict['accepted'] and not new_errors:
                        report['pruned']={'removed':removed,'original_reasons':verdict['reasons'],
                                          'kept_posts':len(new_draft.posts)}
                        report.update(new_verdict); report['statistics']=new_stats
                        accepted_draft=new_draft
        accepted=report['accepted']; dropped=not accepted and state['attempts']>self.config.style.get('max_revisions',1)
        update={'reviews':state.get('reviews',[])+[report], 'accepted':accepted,
                'status':'dropped' if dropped else 'running','reasons':report['reasons']}
        if accepted and report.get('pruned'):
            update['draft']=accepted_draft.model_dump()
            update['drafts']=state.get('drafts',[])+[{'attempt':state['attempts'],'draft':accepted_draft.model_dump(),
                                                      'safe_repairs':[{'pruned':report['pruned']['removed']}]}]
        return update

    async def visual(self,state):
        slot=Slot.model_validate(state['slot']); evidence=state['evidence']
        # The visual agent only picks a layout and a symbol. It does not need the full source text: sending it made
        # marketing requests exceed the provider's request limit (HTTP 413). Send ids and short previews instead.
        cited=[ref for post in state['draft']['posts'] for ref in post['evidence_ids'] if ref in evidence]
        ids=list(dict.fromkeys(cited)) or list(evidence)
        # Written briefs use approved templates; no model request or quota needed.
        topic=state['topic'].lower()
        layout,symbol=('cycle','loop') if any(w in topic for w in ('oracle','swap','slippage')) else ('barrier','gate')
        if self.config.post_type(slot)['source_policy']=='marketing':
            layout,symbol='flow','arrows'
        concept=VisualIdea(layout=layout,symbol=symbol,source_id=ids[0])
        style=self.config.visual
        visual={'type':'written_image_description_only','prompt':' '.join([
                    style['style'],f"Topic: {state['topic']}.",
                    style['layouts'][concept.layout],style['symbols'][concept.symbol],
                    style['text_policy']]),
                'alt_text':f"Illustration about {state['topic']} with {concept.symbol}.",
                'source_id':concept.source_id, 'image_generated':False}
        return {'visual':visual,'status':'shipped','reasons':[]}

    async def run_piece(self,slot,previous=None):
        if previous and previous.get('status')=='failed':
            state=dict(previous,status='running',reasons=[])
            if not state.get('evidence'):
                state.update(await self.wrap('research',self.research)(state))
            while state.get('status')=='running' and not state.get('accepted'):
                if not state.get('draft') or (state.get('reviews') and
                        state['reviews'][-1]['attempt']==state.get('attempts')):
                    state.update(await self.wrap('write',self.write)(state))
                if state.get('status')!='running': break
                state.update(await self.wrap('review',self.review)(state))
            if state.get('status')=='running' and state.get('accepted'):
                state.update(await self.wrap('visual',self.visual)(state))
            return state
        initial={'slot':slot.model_dump()}
        if HAS_LANGGRAPH:
            return await self.graph.ainvoke(initial,config={'recursion_limit':20})
        # Minimal sequential execution for offline validation in environments without LangGraph.
        state=dict(initial)
        for name in ('research','write','review','visual'):
            if name=='visual' and not state.get('accepted'):break
            if name in ('write','review') and state.get('status') in ('failed','dropped'):break
            state.update(await self.wrap(name,getattr(self,name))(state))
            if name=='review' and not state['accepted'] and state.get('status')=='running':
                state.update(await self.wrap('write',self.write)(state))
                state.update(await self.wrap('review',self.review)(state))
        return state


def recent_finding_ids(root, runs=6):
    """Finding ids that shipped in the most recent runs next to this one (used to vary the weekly topics)."""
    ids=set()
    folders=[p for p in Path(root).glob('*') if (p/'resolved_plan.json').is_file()]
    for folder in sorted(folders,key=lambda p:p.stat().st_mtime,reverse=True)[:runs]:
        try:
            plan=json.loads((folder/'resolved_plan.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):
            continue
        for slot in plan.get('slots',[]):
            if slot.get('resolution_status')=='shipped':
                ids.update(slot.get('source_ids',[]))
    return ids


def load_reusable(folder):
    """Approved pieces of an earlier run, so a retry does not pay again for work that already passed review."""
    try:
        pieces=json.loads((Path(folder)/'pieces.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):
        raise ValueError('The run to reuse has no readable pieces.json')
    return {p['slot']['id']:p for p in pieces if p.get('status') in ('shipped','failed')}


async def run_week(plan:Plan,config:Config,provider,web:WebResearch,output:Path,concurrency=2,notify=None,reuse=None):
    if not 1<=concurrency<=8:raise ValueError('Concurrent pieces must be 1..8')
    config.validate_plan(plan)
    if provider.mode=='live' and not HAS_LANGGRAPH:
        raise RuntimeError('LangGraph is required for Live mode; install requirements.txt')
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    runner=Pipeline(config,provider,web,output,notify,recent_ids=recent_finding_ids(output.parent))
    reusable=load_reusable(reuse) if reuse else {}
    if reuse:
        try:
            prior_summary=json.loads((Path(reuse)/'run_summary.json').read_text(encoding='utf-8'))
            current_hashes={name:item['sha256'] for name,item in config.data_snapshot().items()}
            if prior_summary.get('mode')!=provider.mode or prior_summary.get('config_sha256')!=current_hashes:
                reusable={}
        except (OSError,ValueError):
            reusable={}
    reused_ids=[]
    started=utcnow(); sem=asyncio.Semaphore(concurrency)
    async def work(slot):
        previous=reusable.get(slot.id)
        if previous and Slot.model_validate(previous['slot']).model_dump()!=slot.model_dump():
            previous=None
        if previous and previous.get('status')=='shipped':
            reused_ids.append(slot.id)       # same slot, already approved: keep it, spend no tokens
            return dict(previous,resumed_from=Path(reuse).name)
        async with sem:
            try:return await runner.run_piece(slot,previous)
            except Exception as exc:
                runner.event(slot.id,'pipeline','failed',detail=type(exc).__name__)
                return {'slot':slot.model_dump(),'status':'failed',
                        'reasons':['Pipeline exception: '+type(exc).__name__]}
    results=await asyncio.gather(*(work(slot) for slot in plan.slots))
    ok=[r for r in results if r.get('status')=='shipped']
    warnings=week_checks(results,config,provider.mode)
    # Preserve previous usage/cost observability without another Python module.
    complete=all(x.get('input_tokens') is not None and x.get('output_tokens') is not None
                 for x in provider.calls)
    cost_known=all(x.get('cost_usd') is not None for x in provider.calls)
    usage={'model':provider.model,'requests':len(provider.calls),
           'provider_settings':provider.settings_snapshot(),
           'cost_usd':round(sum(x['cost_usd'] for x in provider.calls),8) if cost_known else None,
           'cost_status':'known' if cost_known else 'unknown: provider rates or usage not available',
           'token_totals_complete':complete,
           'calls':provider.calls,
           'input_tokens':sum(x.get('input_tokens') or 0 for x in provider.calls),
           'output_tokens':sum(x.get('output_tokens') or 0 for x in provider.calls),
           'rate_limited':sum(x.get('status')=='rate_limited' for x in provider.calls),
           'note':'Demo counts are fixture-only, not live LLM calls' if provider.mode=='demo' else None}
    summary={'started_at':started,'finished_at':utcnow(),'mode':provider.mode,
             'research_mode':web.mode,'pieces_requested':len(results),'pieces_shipped':len(ok),
             'pieces_not_shipped':len(results)-len(ok), 'pieces_dropped':sum(r.get('status')=='dropped' for r in results),
             'pieces_failed':sum(r.get('status')=='failed' for r in results),
             'failures':[{'slot_id':r['slot']['id'], 'status':r.get('status'),
                          'reasons':r.get('reasons',[])} for r in results if r.get('status')!='shipped'],
             'warnings':warnings,'reused_pieces':sorted(reused_ids),
             'usage':usage,'langgraph_used':HAS_LANGGRAPH,
             'config_sha256':{name:item['sha256'] for name,item in config.data_snapshot().items()},
             'disclaimer':'Demo data is not current web research and must not be published' if provider.mode=='demo' else None}
    # Save structured visual briefs separately; export the actual written prompt in CSV.
    for result in ok:
        relative='visuals/'+result['slot']['id']+'.json'
        result['visual_path']=relative
        save_json(output/relative,result['visual'])
    # First-class, independently readable audit artifacts expected by the task.
    resolved_plan = {'slots': [dict(r['slot'], resolved_topic=r.get('topic'),
                       resolution_status=r.get('status'),
                       source_ids=r.get('research',{}).get('source_ids',[]),
                       sources=r.get('research',{}).get('sources',[])) for r in results]}
    research_log = {'queries':runner.query_log,
                    'resolved_sources':[{'slot_id':r['slot']['id'],
                      'mode':r.get('research',{}).get('mode'),
                      'source_ids':r.get('research',{}).get('source_ids',[]),
                      'sources':r.get('research',{}).get('sources',[]),
                      'provenance':r.get('research',{}).get('provenance',{}),
                      'failure_reason':r.get('reasons',[]) if r.get('status')=='failed' else []}
                      for r in results]}
    drafts_report = {'pieces':[{'slot_id':r['slot']['id'],
                      'attempts':r.get('drafts',[])} for r in results]}
    review_report = {'pieces':[{'slot_id':r['slot']['id'],'status':r.get('status'),
                      'reviews':r.get('reviews',[]), 'reasons':r.get('reasons',[])}
                      for r in results]}
    final_content={'approved':[{'slot_id':r['slot']['id'], 'type':r['slot']['type'],
                  'topic':r['topic'], 'posts':r['draft']['posts'],
                  'image_description':r['visual']['prompt'],
                  'image_generated':False,'sources':r['research']['sources']}
                  for r in ok]}
    save_json(output/'resolved_plan.json', resolved_plan)
    save_json(output/'research_log.json', research_log)
    save_json(output/'drafts.json', drafts_report)
    save_json(output/'review_report.json', review_report)
    save_json(output/'final_content.json', final_content)
    # User-facing, offline content pack: only approved tweets and visual prompts.
    write_delivery_html(final_content, output/'content_delivery.html')
    save_json(output/'run_summary.json', summary)
    save_json(output/'kb_normalized.json', {'findings':config.normalized_kb})
    save_json(output/'summary.json',summary)
    save_json(output/'input_snapshot.json',config.data_snapshot())
    save_json(output/'pieces.json',results)
    save_json(output/'research-log.json',runner.query_log)
    save_json(output/'events.json',runner.events)
    # Machine-readable contracts: one small artifact for developers and downstream tools.
    save_json(output/'schemas.json',{
        'plan':Plan.model_json_schema(),
        'draft':Draft.model_json_schema(),
        'judgment':Judgment.model_json_schema(),
        'visual_choice':VisualIdea.model_json_schema(),
    })
    with (output/'posts.csv').open('w',encoding='utf-8',newline='') as handle:
        w=csv.DictWriter(handle,fieldnames=['Post Text','Image'])
        w.writeheader()
        for result in ok:
            w.writerow({'Post Text':'\n\n'.join(x['text'] for x in result['draft']['posts']),
                        'Image':result['visual']['prompt']})
    return summary
