"""One shared budget for all agents using a free-tier provider in this process."""
import asyncio
import math
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def retry_after_seconds(headers):
    """HTTP Retry-After supports either seconds or an HTTP date."""
    value = headers.get('retry-after') or headers.get('Retry-After')
    if value is None:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class TokenGate:
    """Serialize budgeted API calls and reuse measured token usage after each call.

    Reservations are estimates, not a guarantee of a provider's token counting.
    Counting from completion time and keeping a 61-second window is conservative.
    Unbudgeted providers retain concurrency. A server cooldown is shared by all
    agents, including when local token budgeting is disabled.
    """
    def __init__(self, budget=0, window=61.0, clock=time.monotonic, sleep=asyncio.sleep):
        self.budget = budget
        self.window = window
        self.clock = clock
        self.sleep = sleep
        self.history = deque()
        self.lock = asyncio.Lock()
        self.blocked_until = 0.0
        self.notify = lambda event: None

    def cooldown(self, seconds):
        self.blocked_until = max(self.blocked_until, self.clock() + seconds)

    def record(self, tokens):
        if self.budget:
            self.history.append((self.clock(), tokens))

    async def wait(self, reservation, label):
        total_wait = 0.0
        while True:
            now = self.clock()
            while self.history and now - self.history[0][0] >= self.window:
                self.history.popleft()
            delay = max(0.0, self.blocked_until - now)
            if self.budget and self.history:
                needed = min(reservation, self.budget)
                if sum(tokens for _, tokens in self.history) + needed > self.budget:
                    delay = max(delay, self.history[0][0] + self.window - now)
            if delay <= 0:
                return total_wait
            print(f'[{label}] Provider budget: waiting {delay:.1f}s...', flush=True)
            self.notify({'event': 'waiting', 'agent': label, 'seconds': round(delay, 1)})
            await self.sleep(delay)
            total_wait += delay

    @asynccontextmanager
    async def request(self, reservation, label):
        if self.budget:
            async with self.lock:
                waited = await self.wait(reservation, label)
                yield waited
        else:
            waited = await self.wait(reservation, label)
            yield waited


"""Live LangChain adapter with independently budgeted transport retries."""
import json
import os
import random
import time
from urllib.parse import urlparse


class ProviderFailure(RuntimeError):
    """Contains only an internally generated, safe diagnostic message."""


def runtime_options(env_path):
    """Snapshot current local settings per run; never retain dotenv keys in os.environ.

    Explicit local values (including blanks) override inherited environment values.
    No credentials are logged or persisted in run artifacts.
    """
    from dotenv import dotenv_values
    values=dict(os.environ)
    values.update({key: value for key,value in dotenv_values(env_path).items() if value is not None})
    return values


def safe_error_code(exc):
    """Extract a small provider error *code* without persisting sensitive bodies."""
    code=getattr(exc, 'code', None)
    body=getattr(exc,'body',None)
    if not isinstance(code,str) and isinstance(body,dict):
        error=body.get('error')
        if isinstance(error,dict):
            code=error.get('code')
        else:
            code=body.get('code')
    known={'json_validate_failed','context_length_exceeded',
           'insufficient_quota','billing_hard_limit_reached'}
    return code if isinstance(code,str) and code in known else None


def setting(name, default, minimum=0, integer=False, values=None):
    raw = (os.environ if values is None else values).get(name)
    value = float(default if raw is None or raw == '' else raw)
    if not math.isfinite(value) or value < minimum or (integer and not value.is_integer()):
        raise ValueError(f'Invalid setting: {name}')
    return int(value) if integer else value


class LiveProvider:
    mode = 'live'

    def __init__(self, options=None, progress=None):
        values = dict(os.environ)
        values.update(options or {})
        self._values = values
        self.progress = progress or (lambda event: None)
        get = values.get
        number = lambda name, default, minimum=0, integer=False: setting(name, default, minimum, integer, values)
        if not get('OPENAI_API_KEY'):
            raise ValueError('OPENAI_API_KEY is required for live mode; use --mode demo for offline fixtures')
        self.model = get('LLM_MODEL', 'gpt-4.1-mini')
        is_groq = urlparse(get('OPENAI_BASE_URL', '')).hostname == 'api.groq.com'
        self.output_limit = number('LLM_MAX_COMPLETION_TOKENS', 4096, 256, integer=True)
        self.retries = number('LLM_RATE_LIMIT_RETRIES', 3, integer=True)
        self.max_wait = number('LLM_MAX_RETRY_WAIT_SECONDS', 300, 1)
        self.base_wait = number('LLM_RETRY_BASE_SECONDS', 61, 1)
        self.gate = TokenGate(number('LLM_TOKENS_PER_MINUTE', 7500 if is_groq else 0, integer=True))
        kwargs = {'model': self.model, 'timeout': 60, 'max_retries': 0,
                  'max_completion_tokens': self.output_limit,
                  'api_key': get('OPENAI_API_KEY'),
                  'base_url': get('OPENAI_BASE_URL') or 'https://api.openai.com/v1'}
        # gpt-oss models "think" before answering and that thinking is billed as output tokens: a judge call used 3,000+
        # tokens and a full week exhausted Groq's daily allowance. The exact-quote audit is done by code, so "low" is
        # enough. Override with LLM_REASONING_EFFORT (or LLM_REASONING_EFFORT_<ROLE>) when you want deeper review.
        self._kwargs = kwargs
        self._llms = {}
        self.gate.notify = self.progress
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise ValueError('Live mode needs langchain-openai: pip install -r requirements.txt') from exc
        self._ChatOpenAI = ChatOpenAI
        self.llm = self._llm_for('default')
        self.blocked_until = 0.0         # set when the provider says a limit resets later than we are willing to wait
        self.calls = []
        self.authentication_error = ''
        self.rates = [self._rate('INPUT_USD_PER_MILLION'), self._rate('OUTPUT_USD_PER_MILLION')]

    def _llm_for(self, role):
        """One client per (model, reasoning effort). LLM_MODEL_<ROLE> routes a role to its own model, which also gives
        that role its own provider quota (Groq counts tokens per model)."""
        model = self._values.get(f'LLM_MODEL_{role.upper()}') or self.model
        effort = (self._values.get(f'LLM_REASONING_EFFORT_{role.upper()}') or self._values.get('LLM_REASONING_EFFORT')
                  or ('low' if 'gpt-oss' in model else ''))
        key = (model, effort)
        if key not in self._llms:
            kwargs = dict(self._kwargs, model=model)
            if effort:
                kwargs['reasoning_effort'] = effort
            self._llms[key] = self._ChatOpenAI(**kwargs)
        return self._llms[key]

    def _model_for(self, role):
        return self._values.get(f'LLM_MODEL_{role.upper()}') or self.model

    def _rate(self, name):
        if self._values.get(name) in (None, ''):
            return None
        return setting(name, 0, values=self._values)

    def settings_snapshot(self):
        return {'tokens_per_minute': self.gate.budget,
                'max_completion_tokens': self.output_limit,
                'rate_limit_retries': self.retries,
                'max_retry_wait_seconds': self.max_wait,
                'retry_base_seconds': self.base_wait}

    async def generate(self, role, prompt, payload, schema, slot):
        if self.authentication_error:
            raise ProviderFailure(self.authentication_error)
        if self.blocked_until > time.monotonic():
            # The provider already told us its limit resets later than we will wait. Do not hit it again.
            minutes = max(1, round((self.blocked_until - time.monotonic()) / 60))
            raise ProviderFailure(f'{role} provider failure: token limit reached, resets in about {minutes} min. '
                                  'Use "Retry failed pieces" then; approved pieces are kept')
        text = json.dumps(payload, ensure_ascii=False)
        # Approximate English/JSON input + schema overhead, then reserve the
        # configured maximum completion. Server limits remain authoritative.
        input_bytes = len((prompt + text + json.dumps(schema.model_json_schema())).encode('utf-8'))
        # Reserving the global 4096-token completion limit for every role made
        # short research/visual calls consume the same budget as a reviewer.
        # Keep the provider cap unchanged, but reserve a role-appropriate upper
        # bound so the local gate does not add avoidable 61-second waits.
        role_completion = {
            'research': 768,
            'visual': 768,
            'writer': 2400,
            'reviewer': 2800,
        }.get(role, self.output_limit)
        role_completion = min(self.output_limit, role_completion)
        reservation = math.ceil(input_bytes / 3) + 256 + role_completion
        from langchain_core.messages import HumanMessage, SystemMessage
        contract = '\n\nReturn JSON conforming to this complete JSON Schema:\n' + json.dumps(schema.model_json_schema())
        messages = [SystemMessage(content=prompt + contract), HumanMessage(content=text)]
        agent = f"writer:{payload['post_type']['name']}" if role == 'writer' else role
        request_id = f'{slot}:{agent}:{len(self.calls) + 1}'
        role_llm = self._llm_for(role)
        runnable = role_llm.with_structured_output(schema, method='json_schema', include_raw=True)
        schema_retry_done=False
        validation_retry_done=False
        # Allow at most one extra attempt for a transient JSON-generation failure.
        for attempt in range(1, self.retries + 3):
            record = {'slot': slot, 'role': role, 'agent': agent, 'model': self._model_for(role),
                      'request_id': request_id, 'transport_attempt': attempt,
                      'input_tokens': None, 'output_tokens': None, 'cost_usd': None}
            async with self.gate.request(reservation, f'{slot}/{agent}') as waited:
                record['budget_wait_seconds'] = round(waited, 3)
                start = time.monotonic()
                try:
                    if self.authentication_error:
                        raise ProviderFailure(self.authentication_error)
                    result = await runnable.ainvoke(messages)
                    raw = result.get('raw')
                    usage = getattr(raw, 'usage_metadata', None) or {}
                    record['input_tokens'] = usage.get('input_tokens')
                    record['output_tokens'] = usage.get('output_tokens')
                    recorded = [record['input_tokens'], record['output_tokens']]
                    self.gate.record(sum(recorded) if all(v is not None for v in recorded) else reservation)
                    if all(v is not None for v in [*self.rates, *recorded]):
                        record['cost_usd'] = (recorded[0] * self.rates[0] + recorded[1] * self.rates[1]) / 1_000_000
                    parsed = result.get('parsed')
                    # json_mode is less strict at the transport layer and some
                    # OpenAI-compatible providers return valid JSON while leaving
                    # LangChain's parsed field empty. Validate it locally so the
                    # fallback remains schema-safe.
                    if parsed is None and raw is not None:
                        content = getattr(raw, 'content', '')
                        if isinstance(content, list):
                            content = ''.join(
                                part.get('text', '') if isinstance(part, dict) else str(part)
                                for part in content
                            )
                        if isinstance(content, str) and content.strip():
                            parsed = schema.model_validate_json(content)
                    if parsed is None:
                        raise ValueError('Structured model response could not be parsed')
                    record['status'] = 'ok'
                    return parsed
                except Exception as exc:
                    record['status'] = 'error'
                    record['error_type'] = type(exc).__name__
                    status = getattr(exc, 'status_code', None)
                    record['http_status'] = status if isinstance(status, int) else None
                    # Both SDK and LangChain-normalized rate-limit exceptions
                    # are supported without storing raw exception text or bodies.
                    limited = status == 429 or 'ratelimit' in type(exc).__name__.lower()
                    response = getattr(exc, 'response', None)
                    headers = getattr(response, 'headers', {}) or {}
                    code=safe_error_code(exc)
                    if code:
                        record['error_code']=code  # sanitized enum only; never raw server message
                    permanent = code in ('insufficient_quota', 'billing_hard_limit_reached')
                    if type(exc).__name__=='ValidationError':
                        record['validation_problems'] = [
                            '.'.join(str(x) for x in e.get('loc', ())) + ': ' + e.get('type', 'invalid')
                            for e in exc.errors()[:12]]
                    if type(exc).__name__=='ValidationError' and not validation_retry_done and attempt < self.retries + 2:
                        # The model returned JSON that parsed but broke the schema (for example a missing field). The
                        # run used to stop here with no second chance. Tell the model exactly which fields were wrong
                        # (paths and rules only, never raw model text) and ask once more.
                        validation_retry_done=True
                        problems='; '.join(record['validation_problems'])
                        messages=messages+[HumanMessage(content='Your previous JSON did not match the schema ('+problems+
                                                        '). Return the complete corrected JSON object only.')]
                        record['status']='validation_retry'; record['will_retry']=True
                        self.progress({'event':'retry','slot':slot,'agent':agent,'reason':'validation_retry','attempt':attempt})
                        continue
                    if status == 401 or 'authentication' in type(exc).__name__.lower():
                        host=urlparse(self._kwargs['base_url']).hostname or 'configured provider'
                        reason=f'Authentication rejected by {host}; check the current OPENAI_API_KEY in .env. This is not a token quota error'
                        self.authentication_error=reason
                        record['will_retry']=False
                    elif isinstance(exc,ProviderFailure):
                        raise
                    elif status == 400:
                        if code=='json_validate_failed' and not schema_retry_done and attempt < self.retries + 2:
                            # A second json_schema request repeats the same failure
                            # for models that emit valid JSON with schema-invalid
                            # fields. Retry once through json_mode and validate the
                            # decoded object locally; never retry the identical
                            # transport request indefinitely.
                            schema_retry_done=True
                            runnable = role_llm.with_structured_output(
                                schema, method='json_mode', include_raw=True)
                            messages = [
                                SystemMessage(content=prompt + contract +
                                    '\n\nOUTPUT CONTRACT: Return one JSON object only. '
                                    'Use exactly the requested field names, no markdown, '
                                    'no commentary, and satisfy every enum, length, and '
                                    'array constraint.'),
                                HumanMessage(content=text),
                            ]
                            record['status']='structured_output_retry'
                            record['fallback_method']='json_mode'
                            record['will_retry']=True
                            self.progress({'event':'retry','slot':slot,'agent':agent,
                                           'reason':'structured_output_retry','attempt':attempt})
                            continue
                        reason = ('HTTP 400: generated JSON failed schema validation' if code=='json_validate_failed'
                                  else 'HTTP 400: invalid model request or incompatible structured output')
                    elif status == 413:
                        reason = ('HTTP 413 request too large; reduce retrieved source excerpts '
                                  'or model output budget')
                    elif limited and not permanent:
                        suggested = retry_after_seconds(headers)
                        delay = (suggested if suggested is not None else self.base_wait * 2 ** (attempt - 1)) + random.uniform(.1, 1)
                        record['retry_after_seconds'] = round(delay, 3)
                        if attempt <= self.retries and delay <= self.max_wait:
                            record['status'] = 'rate_limited'
                            record['will_retry'] = True
                            self.gate.cooldown(delay)
                            print(f'[{slot}/{agent}] Rate limit: retry {attempt}/{self.retries} after at least {delay:.1f}s.', flush=True)
                            self.progress({'event': 'retry', 'slot': slot, 'agent': agent,
                                           'seconds': round(delay, 1), 'attempt': attempt})
                            continue
                        record['will_retry'] = False
                        if delay > self.max_wait:
                            # Long reset = a daily/hourly token allowance, not a per-minute limit. Remember it, tell the
                            # user how long it lasts, and let the other slots fail fast instead of calling again.
                            self.blocked_until = time.monotonic() + delay
                            reason = (f'token limit reached, resets in about {max(1, round(delay / 60))} min. '
                                      'Use "Retry failed pieces" then; approved pieces are kept')
                        else:
                            reason = 'rate-limit retries exhausted'
                    elif permanent:
                        reason = 'provider quota unavailable; check account quota'
                    else:
                        reason = type(exc).__name__
                    raise ProviderFailure(f'{role} provider failure: {reason}') from None
                finally:
                    record['latency_seconds'] = round(time.monotonic() - start, 3)
                    self.calls.append(record)


class DemoProvider:
    """Deterministic OFFLINE fixture. Never claims to be a real LLM judge or web search."""
    mode='demo'
    model='offline-fixture-no-llm'
    rates=[0,0]

    def __init__(self, progress=None):
        self.calls=[]; self.progress=progress or (lambda event: None)

    def settings_snapshot(self):
        return {'note':'Offline fixture. Zero API calls.'}

    async def generate(self, role, prompt, payload, schema, slot):
        from .config import Pick, Draft, Post, Judgment, Scores, Claim, VisualIdea
        import re
        await asyncio.sleep(.005)
        self.calls.append({'slot':slot,'role':role,'model':self.model, 'agent':role,
                           'input_tokens':0,'output_tokens':0,'status':'fixture',
                           'cost_usd':0,'latency_seconds':0})
        if role=='research':
            return Pick(source_ids=[x['id'] for x in payload['candidates'][:2]],
                        reason='Offline demo: deterministic first candidate')
        if role=='writer':
            ev=payload['evidence']
            if payload['post_type']['source_policy']=='marketing':
                k=next((k for k in ev if k.endswith(':body')),next(iter(ev)))
                txt=ev[k].split('. ')[0].strip().rstrip('.')+'.'
                return Draft(posts=[Post(text=txt, evidence_ids=[k])])
            snippets=[]
            for k,v in ev.items():
                if len(v)<22 or k.endswith((':title',':impact',':protocol')):continue
                for part in re.split(r'(?<=[.!?])\s+|\n+',v):
                    part=part.strip().lstrip('-+=@ ').strip()
                    if len(part)<22:continue
                    if not part.isascii():
                        continue
                    # Extractive character slice: a genuine substring of the original evidence.
                    part=part[:225].strip()
                    if not re.search(r'(?i)\b(?:delve|leverage|robust|crucial|seamless)\b',part):
                        snippets.append((k,part))
                    if len(snippets)>=12: break
                if len(snippets)>=12: break
            if len(snippets)<5:
                raise ValueError('Demo KB fixture lacks five short ASCII evidence passages')
            return Draft(posts=[Post(text=t,evidence_ids=[k]) for k,t in snippets[:5]])
        if role=='reviewer':
            ev=payload['evidence']
            checks=[]
            for u in payload['units']:
                post=payload['draft']['posts'][u['index']]
                k=post['evidence_ids'][0]
                source=ev.get(k,'')
                # A fixture's simple support check is not a semantic LLM evaluation.
                quote=source[:min(70,len(source))]
                checks.append(Claim(unit_id=u['unit_id'], supported=bool(source and len(quote)>=10),
                      evidence_id=k,quote=quote,reason='Offline fixture quote check'))
            return Judgment(approve=all(x.supported for x in checks),
                    scores=Scores(accuracy=4,usefulness=4,clarity=4,brand_fit=4),
                    claims=checks,reasons=[])
        if role=='visual':
            return VisualIdea(layout='flow', symbol='shield', source_id=payload['allowed_source_ids'][0])
        raise ValueError('Unrecognized fixture role')
