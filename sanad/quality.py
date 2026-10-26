"""Code-enforced writing and grounding checks; run BEFORE LLM-as-a-Judge."""
from __future__ import annotations
import re
import statistics
import unicodedata
from urllib.parse import urlsplit
from .config import Claim, Draft, Judgment, Post, Scores
from .skills import select_evidence

URL_RE = re.compile(r'https?://[^\s)]+', re.I)
BARE_URL_RE = re.compile(r'(?<![\w@])(?:www\.)?[a-z0-9][a-z0-9-]*\.(?:com|org|net|io|dev|xyz|gov|edu|app)(?:/[^\s]*)?', re.I)
WORD_RE = re.compile(r"\b[A-Za-z]+(?:'[A-Za-z]+)?\b")
SPLIT_RE = re.compile(r'(?<=[.!?;])\s+')


X_URL_WEIGHT = 23   # X counts every link as 23 characters, whatever its real length


def tweet_length(text: str) -> int:
    """Length as X counts it. The old check used the raw URL length and rejected valid posts."""
    return len(URL_RE.sub('x' * X_URL_WEIGHT, text))


def units(draft: Draft, approved_ctas=()):
    """Meaningful sentences only. Bare citation labels/URLs are not claims.

    A visible [SS-...] citation is a pointer to evidence, not a fact, and should
    never be scored as an unsupported stand-alone sentence by the LLM judge.
    Python still verifies visible citations and fetched links separately.
    """
    result=[]
    suffix = re.compile(r'\s*(?:\[(?:SS-\d+(?:,\s*SS-\d+)*)\]|https?://\S+)\s*$')
    for i,post in enumerate(draft.posts):
        for sentence in SPLIT_RE.split(post.text.strip()):
            clean=sentence.strip()
            if re.fullmatch(r'(?:\[(?:SS-\d+(?:,\s*SS-\d+)*)\]|https?://\S+)',clean):
                continue
            clean=suffix.sub('',clean).strip()
            # An exact brand-approved CTA is editorial text, not an external
            # factual claim. Python still checks quantity and disallowed links.
            if clean.rstrip('.!?').strip().casefold() in {
                c.rstrip('.!?').strip().casefold() for c in approved_ctas
            }:
                continue
            if clean:
                count=sum(x['index']==i for x in result)+1
                result.append({'unit_id':f'p{i+1}s{count}','index':i,'text':clean})
    return result


def style_checks(draft: Draft, slot, config, evidence: dict, fetched_urls: set):
    rules=config.style
    errors=[]; lengths=[]; full=[]
    spec=config.post_type(slot)
    policy=spec['source_policy']
    required_min,required_max=spec['min_posts'],spec['max_posts']
    if not required_min<=len(draft.posts)<=required_max:
        errors.append(f'{slot.type} needs {required_min}..{required_max} posts')
    if policy=='technical' and len(draft.posts)>rules.get('technical_preferred_max_posts', 7):
        errors.append('Technical thread is too long; keep it to the preferred concise maximum')
    allowed_evidence=set(evidence)
    for index,p in enumerate(draft.posts):
        prefix=f'Post {index+1}: '
        text=p.text
        if URL_RE.search(text):
            errors.append(prefix+'links are forbidden in post text; sources are displayed separately')
        for name in re.findall(r"\bSanad[a-z]*Squad\b",text,re.I):
            if name!='SanadSquad':
                errors.append(prefix+'incorrect company name: use SanadSquad exactly')
        full.append(text)
        length=tweet_length(text)
        if length>rules['max_characters']:
            # Say by how much, so the writer can fix it in one retry instead of guessing.
            errors.append(prefix+f"exceeds 280 characters ({length} counted, {length-rules['max_characters']} over; "
                          "a link counts as 23). Cut a clause, do not add words.")
        if re.search(r'\[(?:web|brand|fixture|kb|ext):[^\]]*\]',text):
            errors.append(prefix+'evidence IDs must go in evidence_ids only, never inside the post text')
        if not text.isascii():
            # More conservative old brand policy; catches emoji and curly punctuation.
            errors.append(prefix+'non-ASCII characters/emoji are prohibited by current brand rules')
        if any(c in text for c in rules['banned_characters']):
            errors.append(prefix+'banned punctuation')
        if any(unicodedata.category(c)=='So' for c in text):
            errors.append(prefix+'emoji/symbol forbidden')
        if '#' in text: errors.append(prefix+'hashtags disabled')
        if text.lstrip().startswith(('=','+','-','@')):
            errors.append(prefix+'unsafe spreadsheet formula prefix')
        for w in rules['banned_words']:
            if re.search(r'(?<!\w)'+re.escape(w)+r'(?!\w)',text,re.I):
                errors.append(prefix+f'banned vocabulary: {w}')
        for pattern in rules['banned_constructions']:
            if re.search(pattern,text,re.I):
                errors.append(prefix+'banned construction: '+pattern)
        for url in URL_RE.findall(text):
            clean=url.rstrip('.,;!?)')
            if clean not in fetched_urls:
                errors.append(prefix+'URL was not fetched in this run: '+clean[:80])
        # Bare/www domains are links too; no URL-format bypass.
        without_full_urls=URL_RE.sub(' ',text)
        for bare in BARE_URL_RE.findall(without_full_urls):
            if bare not in rules.get('allowed_code_identifiers',[]):
                errors.append(prefix+'bare domain must be a fully fetched HTTPS URL: '+bare[:80])
        # Reject all unapproved evidence IDs, not only explicit bad URLs.
        try: select_evidence(evidence,p.evidence_ids)
        except ValueError: errors.append(prefix+'unknown/missing evidence_id')
        if policy=='technical' and any(
                ref.endswith((':title', ':impact', ':protocol'))
                for ref in p.evidence_ids):
            errors.append(prefix+'technical factual claims must cite description, recommendation, or root_cause evidence')
        if policy=='technical':
            if any(ref.endswith(':context') for ref in p.evidence_ids) and re.search(
                    r'\b(?:fixed|patched|resolved|remediated)\s+(?:on|by|in)\s+\d',text,re.I):
                errors.append(prefix+'report metadata does not establish a fix date; state the reported status separately from the report date')
            # "...the transaction reverts, protecting the user from front-running" was rejected in run 6: the source
            # states the revert, not the protection. A benefit word must appear in the cited evidence too.
            cited=' '.join(evidence.get(ref,'') for ref in p.evidence_ids)
            for word in rules.get('technical_benefit_words',[]):
                # Only the trailing consequence clause (", protecting the user ..."). Ordinary verbs such as "to prevent"
                # in an instruction are not flagged, and a word the cited evidence itself uses is always allowed.
                trailing=r',\s+'+re.escape(word)+r'(?!\w)'
                if re.search(trailing,text,re.I) and not re.search(r'(?<!\w)'+re.escape(word)+r'(?!\w)',cited,re.I):
                    errors.append(prefix+f'", {word} ..." adds an effect the cited source does not state: '
                                  'end the sentence after the action the source describes')
        if policy=='technical' and re.search(
                r'\b(?:dm\s+to\s+discuss|consider\s+(?:a\s+)?security\s+review|'
                r'request\s+(?:a\s+)?security\s+review|schedule\s+(?:a\s+)?security\s+review|'
                r'review\s+your\b|want\s+(?:a\s+)?security\s+review)\b',
                text, re.I):
            errors.append(prefix+'technical threads must not contain promotional CTAs')
        if policy=='marketing':
            if any(cta.lower() in text.lower() for cta in rules.get('approved_ctas',[])):
                errors.append(prefix+'omit the optional sales CTA; use the space for the sourced observation and service connection')
            # Claims about what SanadSquad does must describe scope (reviews, covers, tests), never promise an
            # outcome. "SanadSquad secures the full stack" was rejected by the judge in every live run.
            overclaim=rules.get('service_overclaim_words',[])
            for sentence in SPLIT_RE.split(text):
                if re.search(r"\b(?:SanadSquad|our|we)\b",sentence,re.I):
                    for word in overclaim:
                        if re.search(r'(?<!\w)'+re.escape(word)+r'(?!\w)',sentence,re.I):
                            errors.append(prefix+f'service overclaim "{word}": say what the service reviews or covers, not what it secures or guarantees')
            if not any(ref.startswith(('web:', 'fixture:')) for ref in p.evidence_ids):
                errors.append(prefix+'must cite a fetched web source (company brand guidance alone is insufficient)')
            # Numeric business claims must appear in sources; guard against invented statistics.
            source_text=' '.join(evidence.get(ref,'') for ref in p.evidence_ids)
            plain=URL_RE.sub(' ',text)
            for number in re.findall(r'(?<![\w.])\d{2,}(?:\.\d+)?(?!\w)',plain):
                if number not in source_text:
                    errors.append(prefix+'numeric claim not in cited evidence: '+number)
        for word in WORD_RE.findall(text):
            if word.lower() in rules['hedging_words']:
                pass
        lengths += [len(WORD_RE.findall(s)) for s in SPLIT_RE.split(text) if s.strip()]
    whole=' '.join(full)
    words=WORD_RE.findall(whole)
    hedges=sum(bool(re.fullmatch('|'.join(map(re.escape,rules['hedging_words'])),w,re.I)) for w in words)
    density=100*hedges/max(len(words),1)
    if density > rules['max_hedges_per_100_words']:
        errors.append(f'Hedging density {density:.1f}/100 exceeds {rules["max_hedges_per_100_words"]}')
    # Count parallel THREE-PART phrases within individual sentences only. The
    # old regex greedily crossed punctuation and confused unrelated clauses.
    triple_samples=[]
    for post in draft.posts:
        for sentence in SPLIT_RE.split(post.text.strip()):
            # Require two commas and a final conjunction, excluding citation IDs.
            sentence=re.sub(r'\[[^]]*\]', '', sentence)
            for match in re.finditer(r'\b([^,.;!?]{3,55}),\s*([^,.;!?]{3,55}),\s*(?:and|or)\s+([^,.;!?]{3,55})',sentence,re.I):
                triple_samples.append(match.group(0).strip())
    repeated_three=len(triple_samples)
    if repeated_three>=2:
        errors.append('Repeated rule-of-three sentence construction. Rewrite at least one: ' +
                      ' | '.join(triple_samples[:2])[:220])
    if policy=='marketing':
        if slot.format=='short-tip' and URL_RE.search(whole):
            errors.append('Short-tip format forbids links')
    if policy=='technical' and slot.source_mode=='kb':
        cited_ids={ref.split(':')[1] for post in draft.posts for ref in post.evidence_ids
                   if ref.startswith('kb:') and len(ref.split(':'))>2}
        if not cited_ids or not any('['+id_ in whole for id_ in cited_ids):
            errors.append('Technical KB thread must visibly cite a real finding ID')
    if policy=='marketing':
        ctas=rules.get('approved_ctas',[])
        if sum(any(cta.lower() in p.text.lower() for cta in ctas) for p in draft.posts)>1:
            errors.append('More than one CTA in a marketing post')
        if slot.format=='short-tip' and len(units(draft))>3:
            errors.append('Short tip may contain only 1-3 sentences')
    # Posts that restate each other read as padding and double the number of claims the judge must support.
    sentence_words=[(i,set(w.lower() for w in WORD_RE.findall(sent) if len(w)>2))
                    for i,post in enumerate(draft.posts) for sent in SPLIT_RE.split(re.sub(r'\[[^]]*\]','',post.text)) if sent.strip()]
    for a in range(len(sentence_words)):
        for b in range(a+1,len(sentence_words)):
            (i,x),(j,y)=sentence_words[a],sentence_words[b]
            if i!=j and len(x)>=4 and len(y)>=4 and len(x&y)/len(x|y)>=0.6:
                errors.append(f'Post {j+1} repeats the point of post {i+1}: every post must add a new fact')
    if re.search(r'\b(?:e\.g|i\.e)\.',whole,re.I):
        errors.append('Avoid "e.g." and "i.e.": they split sentences and read as machine text; write "for example"')
    stats={'hedges_per_100_words':round(density,2), 'sentence_length_variance':
           round(statistics.pvariance(lengths),2) if len(lengths)>1 else None,
           'sentence_lengths_words':lengths, 'repeated_rule_of_three':repeated_three}
    return list(dict.fromkeys(errors)),stats


def evaluate(judgment: Judgment, draft: Draft, evidence: dict, rules: dict):
    errors=['Editorial: '+issue for issue in judgment.editorial_issues]
    records=units(draft, rules.get('approved_ctas', []))
    known={x['unit_id']:x for x in records}
    if sorted(c.unit_id for c in judgment.claims) != sorted(known):
        errors.append('Judge must evaluate each sentence exactly once')
    for claim in judgment.claims:
        if claim.unit_id not in known: continue
        post=draft.posts[known[claim.unit_id]['index']]
        original=evidence.get(claim.evidence_id)
        if claim.supported:
            # Optional extra quotes: each must be exact and from evidence this post cites.
            for extra in claim.extra_citations:
                source=evidence.get(extra.evidence_id)
                if not source or extra.evidence_id not in post.evidence_ids:
                    errors.append(f'{claim.unit_id}: invalid evidence citation')
                elif len(extra.quote)<10 or extra.quote not in source:
                    errors.append(f'{claim.unit_id}: quote is not an exact source substring')
        if not claim.supported:
            errors.append(f'{claim.unit_id}: unsupported ({claim.reason})')
            # An honest judge has no supporting ID/quote to provide. Do not
            # turn that semantic rejection into a second misleading citation
            # error; the unsupported reason is already decisive.
            continue
        if not original or claim.evidence_id not in post.evidence_ids:
            errors.append(f'{claim.unit_id}: invalid evidence citation')
        elif len(claim.quote)<10 or claim.quote not in original:
            errors.append(f'{claim.unit_id}: quote is not an exact source substring')
    grades=judgment.scores.model_dump()
    total=sum(grades.values())
    if not judgment.approve: errors.extend(judgment.reasons or ['Judge rejected'])
    if total<rules['min_reviewer_total'] or min(grades.values())<rules['min_reviewer_each']:
        errors.append('Judge score below quality threshold')
    return {'accepted':not errors,'score':total,'scores':grades,'reasons':list(dict.fromkeys(errors)),
            'judgment':judgment.model_dump()}


def week_checks(results,config,mode):
    """Audit marketing mix after the run, and never silently invent replacement posts."""
    marketing=[r for r in results if config.post_types[r['slot']['type']]['source_policy']=='marketing' and r['status']=='shipped']
    if not marketing: return ['No approved marketing posts']
    notes=[]
    services=[r['slot']['service'] for r in marketing]
    if len(services)!=len(set(services)):
        notes.append('More than one marketing post per service: review current-news justification')
    formats=[r['slot']['format'] for r in marketing]
    if any(formats.count(fmt)>2 for fmt in formats):notes.append('Marketing format repeated more than twice')
    links=[bool(URL_RE.search(' '.join(p['text'] for p in r['draft']['posts']))) for r in marketing]
    cta_count=sum(any(cta.lower() in ' '.join(p['text'].lower() for p in r['draft']['posts'])
                      for cta in config.style['approved_ctas']) for r in marketing)
    if cta_count > len(marketing)*config.style['max_ctas_per_week_fraction']:
        notes.append('Too many calls to action this week')
    if mode=='demo':notes.append('DEMO: fixture sources are not live fetched research; do not publish directly')
    return notes


def prune_unsupported(draft: Draft, judgment: Judgment, evidence: dict, rules: dict, spec: dict, policy: str):
    """Keep the supported part of a nearly-good piece instead of throwing all of it away.

    Live runs lost whole threads because ONE sentence of five was not fully supported. The judge already says which
    sentences those are. Here code removes exactly them, and only when what remains is still a complete piece:

    * technical thread: whole posts are removed; the hook (first) and the closing mitigation (last) are never removed,
      and the thread must keep at least min_posts posts;
    * marketing post: unsupported SENTENCES are removed; at least two sentences must remain.

    Nothing is rewritten. The caller must obtain a fresh model judgment and run
    evaluate() and style checks on the shortened draft. The returned remapped
    judgment is explicitly unapproved. Returns (draft, judgment, removed) or None.
    """
    ctas=rules.get('approved_ctas',[])
    records=units(draft,ctas)
    by_unit={r['unit_id']:r for r in records}
    bad_units={c.unit_id for c in judgment.claims if not c.supported and c.unit_id in by_unit}
    bad_posts=set()
    grades=judgment.scores.model_dump()
    others=[grades[k] for k in ('usefulness','clarity','brand_fit')]
    if min(others)<rules['min_reviewer_each']:
        return None                       # unsupported claims are not the only problem: use the normal retry
    if policy=='technical':
        bad_posts|={by_unit[u]['index'] for u in bad_units}
        n=len(draft.posts)
        if not bad_posts or 0 in bad_posts or n-1 in bad_posts:
            return None
        keep=[i for i in range(n) if i not in bad_posts]
        if len(keep)<spec['min_posts']:
            return None
        remap={old:new for new,old in enumerate(keep)}
        new_draft=Draft(posts=[draft.posts[i] for i in keep])
        def new_id(u):
            index=by_unit[u]['index']
            return None if index not in remap else 'p%ds%s'%(remap[index]+1,u.split('s',1)[1])
        removed={'posts':sorted(i+1 for i in bad_posts),'units':sorted(bad_units)}
    elif policy=='marketing' and len(draft.posts)==1:
        if not bad_units:
            return None
        suffix=re.compile(r'\s*(?:\[(?:SS-\d+(?:,\s*SS-\d+)*)\]|https?://\S+)\s*$')
        bad_text={by_unit[u]['text'] for u in bad_units}
        kept=[sent for sent in SPLIT_RE.split(draft.posts[0].text.strip())
              if suffix.sub('',sent).strip() not in bad_text]
        text=' '.join(kept).strip()
        new_draft=Draft(posts=[Post(text=text,evidence_ids=draft.posts[0].evidence_ids)])
        if len(units(new_draft,ctas))<2:
            return None
        survivors=[r['unit_id'] for r in records if r['unit_id'] not in bad_units]
        order={u:'p1s%d'%(k+1) for k,u in enumerate(survivors)}
        new_id=lambda u: order.get(u)
        removed={'posts':[],'units':sorted(bad_units)}
    else:
        return None
    # Remap claims only as a diagnostic aid. Deletion cannot grant approval or
    # improve scores; the caller must obtain a fresh judgment for this draft.
    claims=[]
    for c in judgment.claims:
        if c.unit_id in bad_units or c.unit_id not in by_unit:
            continue
        nid=new_id(c.unit_id)
        if nid:
            claims.append(Claim(**{**c.model_dump(),'unit_id':nid}))
    scores=Scores(**grades)
    new_judgment=Judgment(approve=False,scores=scores,claims=claims,reasons=['Edited draft requires fresh review'])
    return new_draft,new_judgment,removed
