"""One place for project settings and typed agent contracts."""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Slot(Strict):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]+$', max_length=64)
    type: str = Field(pattern=r'^[a-z][a-z0-9-]{1,63}$')
    focus: str = Field(default='', max_length=240)
    service: str = ''
    services: list[str] = Field(default_factory=list, max_length=2)
    format: str = 'service-overview'
    source_mode: Literal['kb', 'external', 'web'] = 'kb'
    source_url: str = ''
    vulnerability_kb_id: str = ''

    @model_validator(mode='after')
    def check_route(self):
        if self.type == 'marketing-post':
            if len(self.services)!=len(set(self.services)):
                raise ValueError('Cross-service research must not repeat a service')
            if self.source_mode != 'web' or self.source_url:
                raise ValueError('Marketing is routed exclusively through web search; cannot provide a KB/external override')
        elif self.type == 'tech-thread' and self.source_mode == 'web':
            raise ValueError('Technical threads must use KB or external source')
        elif self.source_mode == 'external' and not self.source_url:
            raise ValueError('External technical thread requires source_url')
        return self


class Plan(Strict):
    slots: list[Slot] = Field(min_length=1, max_length=50)

    @model_validator(mode='after')
    def validate_slots(self):
        if len({s.id for s in self.slots}) != len(self.slots):
            raise ValueError('Each slot must have a unique ID')
        marketing=[s for s in self.slots if s.source_mode=='web']
        formats=[s.format for s in marketing]
        if any(formats.count(fmt)>2 for fmt in formats):
            raise ValueError('Marketing weekly mix: no format may appear more than twice')
        services=[s.service for s in marketing]
        if any(services.count(name)>1 for name in services):
            # A current-news exception requires live evidence and manual editorial approval.
            raise ValueError('Marketing weekly mix: repeat a service only after external editorial approval')
        return self


class Post(Strict):
    text: str = Field(min_length=1)
    # No min_length here on purpose: an empty list used to crash the whole writer call with a
    # pydantic ValidationError. quality.style_checks now reports it as precise feedback instead.
    evidence_ids: list[str]


class Draft(Strict):
    posts: list[Post] = Field(min_length=1)


class Pick(Strict):
    source_ids: list[str] = Field(min_length=1, max_length=3)
    reason: str


class Citation(Strict):
    evidence_id: str
    quote: str


class Claim(Strict):
    unit_id: str
    supported: bool
    evidence_id: str
    quote: str
    reason: str
    # A sentence may need more than one passage (for example a two-step attack example).
    # The judge may add up to two more exact quotes; together they must support the sentence.
    extra_citations: list[Citation] = Field(default_factory=list, max_length=2)


class Scores(Strict):
    accuracy: int = Field(ge=0, le=5)
    usefulness: int = Field(ge=0, le=5)
    clarity: int = Field(ge=0, le=5)
    brand_fit: int = Field(ge=0, le=5)


class Judgment(Strict):
    approve: bool
    scores: Scores
    claims: list[Claim]
    reasons: list[str]
    editorial_issues: list[str] = Field(default_factory=list, description='Blocking publication issues: repeated ideas with post numbers, omitted attack prerequisites, misleading chronology, vague subject, or unattributed source self-claims. Empty only when none remain.')


class VisualIdea(Strict):
    layout: Literal['flow', 'barrier', 'cycle']
    symbol: Literal['arrows', 'gate', 'loop', 'shield']
    source_id: str


def parse_prompts(markdown: str, required=None) -> dict[str, str]:
    """Five editable Markdown sections, one per agent role. No extra prompt files."""
    allowed = {'research', 'tech_writer', 'marketing_writer', 'reviewer', 'visual'} if required is None else set(required)
    result = {}
    current = None
    lines = []
    def flush():
        if current is not None:
            value = '\n'.join(lines).strip()
            if not value:
                raise ValueError(f'Empty agent prompt: {current}')
            result[current] = value
    for line in markdown.splitlines():
        if line.startswith('## '):
            flush()
            name = line[3:].strip()
            if name not in allowed or name in result:
                raise ValueError(f'Unknown or repeated prompt section: {name}')
            current, lines = name, []
        elif current is not None:
            lines.append(line)
    flush()
    if result.keys() != allowed:
        raise ValueError('Missing agent prompt sections: ' + ', '.join(sorted(allowed - result.keys())))
    return result


class Config:
    """Read source files once per run; no mutable global config."""
    def __init__(self, root: Path = ROOT):
        self.root = Path(root).resolve()
        self.data = self.root / 'data'
        # Snapshot everything once so a run cannot mix edited configuration versions.
        self._source_text={p.name:p.read_text(encoding='utf-8-sig')
                           for p in self.data.iterdir() if p.is_file()}
        def read(name):
            return self._source_text[name]
        self.read = read
        self.kb_raw = json.loads(read('kb.json'))
        self.kb = self.kb_raw['findings']
        if len(self.kb) < 15 or len({e['id'] for e in self.kb}) != len(self.kb):
            raise ValueError('KB must contain at least 15 unique findings')
        self.kb_by_id = {e['id']: e for e in self.kb}
        self.sources = json.loads(read('sources.json'))
        self.services = self.sources['services']
        if len(self.services) < 3 or not self.sources.get('_deny'):
            raise ValueError('Source policy needs at least three service lines and a deny list')
        self.post_types = json.loads(read('post-types.json'))
        if not self.post_types: raise ValueError('Define at least one content type in post-types.json')
        for name, spec in self.post_types.items():
            if not re.fullmatch(r'[a-z][a-z0-9-]{1,63}', name):
                raise ValueError('Invalid post type name: ' + name)
            if spec.get('source_policy') not in ('technical','marketing'):
                raise ValueError('Invalid source_policy in post-types.json: ' + name)
            if not isinstance(spec.get('min_posts'),int) or not isinstance(spec.get('max_posts'),int) or not 1<=spec['min_posts']<=spec['max_posts']<=10:
                raise ValueError('Post type count must be between 1 and 10: ' + name)
            if not re.fullmatch(r'[a-z][a-z0-9_]*',str(spec.get('writer_prompt',''))):
                raise ValueError('Invalid writer_prompt in post-types.json: ' + name)
        self.style = json.loads(read('style.json'))
        self.brand = read('brand-voice.md')
        match = re.search(r'```json\s*(.*?)\s*```', self.brand, re.S)
        if not match: raise ValueError('brand-voice.md must contain its JSON rules block')
        brand_rules=json.loads(match.group(1))
        # Existing brand restrictions remain enforced by Python, not only the prompt.
        self.style['banned_words']=sorted(set(self.style['banned_words']+brand_rules['banned_words']))
        self.style['banned_characters']=sorted(set(self.style['banned_characters']+brand_rules['banned_characters']))
        self.style['approved_ctas']=list(dict.fromkeys(self.style['approved_ctas']+brand_rules['approved_ctas']))
        self.style['ascii_only']=brand_rules.get('ascii_only',False)
        self.marketing = read('marketing-rules.md')
        self.visual = json.loads(read('visual-style.json'))
        self.prompts = parse_prompts(read('prompts.md'), {'research','reviewer','visual'} | {x['writer_prompt'] for x in self.post_types.values()})
        for role in ('research','tech_writer','marketing_writer','reviewer','visual'):
            if not self.prompts.get(role):
                raise ValueError(f'Missing agent prompt: {role}')
        self.plan = Plan.model_validate_json(read('plan.json'))
        self.validate_plan(self.plan)
        self.kb_root_causes = json.loads(read('root-causes.json'))
        self.normalized_kb = []
        for finding in self.kb:
            item = self.kb_root_causes.get(finding['id'])
            if item is None:
                raise ValueError('Missing curated root cause for finding: '+finding['id'])
            if item['evidence_quote'] not in finding['content']['description']:
                raise ValueError('Root cause quote is not a literal substring of uploaded finding '+finding['id'])
            self.normalized_kb.append({
                'id': finding['id'], 'name':finding['title'], 'severity':finding['impact'],
                'category':finding.get('category',[]), 'description':finding['content']['description'],
                'root_cause':item['root_cause'], 'root_cause_evidence_quote':item['evidence_quote'],
                'source_report':finding.get('source',{}).get('report_name','')})

    def post_type(self, slot: Slot):
        if slot.type not in self.post_types:
            raise ValueError('Undefined content type: '+slot.type+'; add it to data/post-types.json')
        return self.post_types[slot.type]

    def validate_plan(self, plan: Plan):
        marketing=[]
        for slot in plan.slots:
            policy=self.post_type(slot)['source_policy']
            if policy=='marketing':
                if slot.source_mode!='web' or slot.source_url:
                    raise ValueError(f'{slot.id}: marketing requires web-only research')
                if slot.service not in self.services or any(s not in self.services for s in slot.services):
                    raise ValueError(f'{slot.id}: unknown marketing service')
                marketing.append(slot)
            elif slot.source_mode=='web' or (slot.source_mode=='external' and not slot.source_url):
                raise ValueError(f'{slot.id}: technical must use KB or a supplied external URL')
        formats=[s.format for s in marketing]
        if any(formats.count(fmt)>2 for fmt in formats):
            raise ValueError('Weekly marketing mix: no format may appear more than twice')
        services=[s.service for s in marketing]
        if len(services)!=len(set(services)):
            raise ValueError('Weekly marketing mix: repeated service requires manual approval')
        return plan

    def data_snapshot(self):
        import hashlib
        return {name:{'sha256':hashlib.sha256(content.encode('utf-8')).hexdigest(),
                      'content':content} for name,content in self._source_text.items()}
