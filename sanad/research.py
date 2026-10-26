"""Enforced source routing: tech KB/external, marketing allowlisted live web ONLY."""
from __future__ import annotations
import ipaddress
import asyncio
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
import httpx
from .skills import entry_evidence  # shared knowledge skill (also used by Review)


class SourceError(ValueError):
    pass


class PlainHTML(HTMLParser):
    """Skip template/navigation boilerplate before extracting cited page text."""
    EXCLUDED={'script','style','svg','noscript','nav','header','footer','aside'}

    def __init__(self):
        super().__init__()
        self.parts=[]
        self.tags=[]
        self.excluded_depth=0
        self.article_parts=[]
        self.main_parts=[]

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag in self.EXCLUDED:
            self.excluded_depth+=1

    def handle_startendtag(self, tag, attrs):
        # Do not leave a phantom open tag for self-closed tags.
        pass

    def handle_endtag(self, tag):
        if tag in self.tags:
            while self.tags:
                popped=self.tags.pop()
                if popped in self.EXCLUDED:
                    self.excluded_depth=max(0,self.excluded_depth-1)
                if popped==tag:
                    break

    def handle_data(self, data):
        if self.excluded_depth==0 and data.strip():
            self.parts.append(data.strip())
            if 'article' in self.tags: self.article_parts.append(data.strip())
            if 'main' in self.tags: self.main_parts.append(data.strip())

    def text(self):
        for parts in (self.article_parts,self.main_parts,self.parts):
            text=' '.join(parts)
            if len(text)>=100: return text
        return ' '.join(self.parts)


def _entry_matches(host, path, entry):
    """An allowlist entry is 'host' or 'host/path-prefix' (for example github.com/ethereum).

    Whole-host entries for github.com let the search return random repositories, topic pages and
    awesome-lists, which then crowded out real sources. A path prefix keeps only the named organisation.
    """
    entry_host, _, prefix = entry.lower().partition('/')
    if not (host == entry_host or host.endswith('.' + entry_host)):
        return False
    return not prefix or (path.lower() + '/').startswith('/' + prefix.rstrip('/') + '/')


def host_allowed(url, domains, deny):
    """Host-only exact/subdomain match prevents evil-example.com bypasses."""
    try:
        split = urlsplit(url)
        host = (split.hostname or '').lower().rstrip('.')
        if split.scheme != 'https' or split.username or split.password or split.port not in (None,443):
            return False
        if not host or '.' not in host: return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        if any(host == h or host.endswith('.' + h) for h in deny): return False
        return any(_entry_matches(host, split.path, entry) for entry in domains)
    except (ValueError, TypeError):
        return False


def service_domains(config, service):
    if service not in config.services:
        raise SourceError(f'Unknown service: {service}')
    groups = config.services[service]
    return sorted({domain.lower() for values in groups.values() for domain in values})


def candidate_kb(config, focus, reserved=None, avoid=None):
    """Find named findings and tagged concepts. Never silently invent a KB match.

    `avoid` holds finding ids shipped in recent runs. Without it, a topic such as "Access Control" resolved to the same
    finding every week and the thread read like the last one. Fresh findings come first; a recent one is still used
    when nothing else matches.
    """
    reserved = reserved or set()
    avoid = avoid or set()
    available = [k for k in config.kb if k['id'] not in reserved]
    if not focus.strip():
        return ([k for k in available if k['id'] not in avoid] or available)[:3]
    terms = re.findall(r'[a-z0-9]+', focus.lower())
    scored = []
    for k in available:
        searchable = ' '.join(map(str,[k['id'],k['title'],k.get('impact',''),k.get('protocol',''),
                                            ' '.join(k.get('tags',[])), ' '.join(k.get('category',[]))])).lower()
        hits = sum(term in searchable for term in terms)
        if hits:
            scored.append((hits / max(len(terms),1), hits, k))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]['id']))
    matches = [x[2] for x in scored[:5] if x[0] >= 0.5]
    fresh = [k for k in matches if k['id'] not in avoid]
    return fresh if len(matches) > 1 and fresh else matches


async def fetch_url(url, domains, deny, client=None):
    if not host_allowed(url, domains, deny):
        raise SourceError('URL is not in the allowed HTTPS domains (or is denied)')
    async def request(http):
        current = url
        for _ in range(4):
            if not host_allowed(current, domains, deny):
                raise SourceError('Redirect target is outside the allowed HTTPS domains')
            async with http.stream('GET', current, follow_redirects=False,
                                   headers={'User-Agent': 'Mozilla/5.0 (compatible; SanadSquadResearch/1.0)', 'Accept': 'text/html,text/plain;q=0.9'}) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('location')
                    if not location:
                        raise SourceError('Source redirect had no location')
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise SourceError(f'Source fetch returned HTTP {response.status_code}')
                typ = response.headers.get('content-type','').lower()
                if 'html' not in typ and 'text/plain' not in typ:
                    raise SourceError('Non-text source cannot be grounded (' + (typ.split(';')[0] or 'unknown type') + ')')
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 600000: raise SourceError('Source too large')
                body = data.decode('utf-8', errors='replace')
                if 'html' in typ:
                    parser = PlainHTML(); parser.feed(body); body = parser.text()
                body = re.sub(r'\s+', ' ', body).strip()
                if len(body) < 100:
                    raise SourceError('Source had insufficient readable content')
                return body[:10000]
        raise SourceError('Source redirected too many times')
    if client is not None: return await request(client)
    async with httpx.AsyncClient(timeout=17) as http:
        return await request(http)


SERVICE_TERMS = {
    'web3-security': {'smart contract', 'solidity', 'audit', 'blockchain', 'solana', 'ethereum', 'web3', 'security', 'vulnerability'},
    'pentesting': {'supply chain', 'dependency', 'dependencies', 'ci/cd', 'build pipeline', 'owasp', 'cisa', 'pentest', 'application security', 'security'},
    'agentic-security': {'agent', 'agentic', 'prompt injection', 'llm', 'model security', 'ai security', 'tool abuse'},
    'agentic-payments': {'x402', 'mpp', 'payment', 'facilitator', 'transaction policy', 'agent wallet'},
}


# Words that appear on almost every security-related page. They must never be the only reason to read a page:
# an Immunefi bug-bounty info page and a GitHub topic listing both matched on "security" / "audit".
GENERIC_TERMS = {'security', 'audit', 'blockchain', 'web3', 'agent', 'payment'}
GENERIC_FOCUS = {'audit', 'security', 'coverage', 'risk', 'web3', 'guidance', 'report', 'analysis', 'recent', 'release'}
DEFAULT_NOISE_URLS = [r'/topics?/', r'/jobs?/', r'jobs\.', r'/careers?', r'explorer\.', r'/issues/', r'/pull/',
                      r'/podcasts?/', r'/address/', r'/block/']


def noise_url(url: str, patterns=None) -> bool:
    """Listing, job, explorer and issue pages are never evidence. Patterns live in data/sources.json."""
    return any(re.search(p, url, re.I) for p in (patterns or DEFAULT_NOISE_URLS))


def body_relevant(body: str, service: str, focus: str) -> bool:
    """Second gate, on the FETCHED page text: at least two service-specific terms, and most of the focus words."""
    low = body.lower()
    specific = [t for t in SERVICE_TERMS.get(service, ()) if t not in GENERIC_TERMS and t in low]
    tokens = [t for t in re.findall(r'[a-z0-9]+', focus.lower()) if len(t) > 3 and t not in GENERIC_FOCUS]
    need = 1.0 if len(tokens) <= 2 else .6        # "Supply chain" must really be on the page, not just one of the words
    focus_ok = not tokens or sum(t in low for t in tokens) / len(tokens) >= need
    return len(specific) >= 2 and focus_ok


def relevant_result(item, service: str, focus: str) -> bool:
    """Cheap conservative relevance gate; *not* proof of factual reliability.

    Tavily results may come from an allowed domain but discuss an unrelated
    subject. In particular github.com matches almost any code-repository query.
    Never pass those unrelated pages as evidence to the writer.
    """
    text=' '.join([str(item.get('title','')),str(item.get('url',''))]).lower()
    summary=str(item.get('content','')).lower()[:900]
    signals={t for t in SERVICE_TERMS.get(service,set()) if t not in GENERIC_TERMS}
    title_hits=sum(term in text for term in signals)
    summary_hits=sum(term in summary for term in signals)
    focused=[token for token in re.findall(r'[a-z0-9]+',focus.lower()) if len(token)>3 and token not in GENERIC_FOCUS]
    focus_hits=sum(token in text or token in summary for token in focused)
    # For very broad hosts, title/URL must connect to this service or focus:
    # page boilerplate often says 'security' regardless of the real topic.
    host=(urlsplit(item.get('url','')).hostname or '').lower()
    # Dedicated vendor/security documentation hosts are themselves topical.
    dedicated={'agentic-security':{'genai.owasp.org'},
               'web3-security':{'docs.soliditylang.org','blog.openzeppelin.com'},
               'pentesting':{'portswigger.net','owasp.org'},
               'agentic-payments':{'docs.cdp.coinbase.com','developers.cloudflare.com'}}
    if host in dedicated.get(service,set()):
        return True
    if host=='github.com' or host.endswith('.github.com'):
        strong=sum(term in text for term in signals if term not in {'security','agent'})
        return strong>0 or (title_hits>0 and focus_hits>=1)
    # A broad focus such as "Web3 audit coverage" has no topical words left once generic ones are removed. Then the page
    # only has to show a service-specific term; requiring a focus hit made every candidate "unrelated" in run 6.
    return (title_hits>=1 or (summary_hits>=1 and (focus_hits>=1 or not focused)))


class WebResearch:
    """Search adapter. Live needs Tavily; demo uses labeled local fixtures (never called live)."""
    def __init__(self, config, mode='live', api_key=None, fixture=None, client=None):
        self.config=config; self.mode=mode; self.api_key=api_key
        self.client=client; self.fixture=fixture or {}
        self._gate=None     # created lazily inside the running event loop

    async def _post(self, payload):
        """One Tavily request. Retries timeouts, 429 and 5xx twice (2 s, 5 s); everything else fails at once."""
        delays=(2,5)
        for attempt in range(len(delays)+1):
            try:
                if self.client is not None:
                    response=await self.client.post('https://api.tavily.com/search',json=payload,timeout=25)
                else:
                    async with httpx.AsyncClient(timeout=25) as http:
                        response=await http.post('https://api.tavily.com/search',json=payload)
                if getattr(response,'status_code',200) in (429,500,502,503,504) and attempt<len(delays):
                    await asyncio.sleep(delays[attempt]); continue
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt<len(delays):
                    await asyncio.sleep(delays[attempt]); continue
                raise

    @staticmethod
    def _explain(exc):
        """A short, safe reason for the log and the UI. Never includes the key or raw response text."""
        code=getattr(getattr(exc,'response',None),'status_code',None)
        if code in (401,403): return f'HTTP {code}: Tavily rejected the API key (check TAVILY_API_KEY)'
        if code in (432,433): return f'HTTP {code}: Tavily plan or credit limit reached'
        if code==429: return 'HTTP 429: Tavily rate limit, still failing after retries'
        if code==400: return 'HTTP 400: Tavily rejected the request'
        if code: return f'HTTP {code} from Tavily'
        return {'ConnectError':'could not connect to Tavily (network or firewall)','ConnectTimeout':'connection to Tavily timed out',
                'ReadTimeout':'Tavily did not answer in time'}.get(type(exc).__name__, type(exc).__name__)

    async def search(self, service, focus, query_log, format="service-overview"):
        domains = service_domains(self.config, service)
        service_name = service.replace('-', ' ')
        focus = focus or service_name
        # Short, topical queries. The old ones repeated "security" three times ("web3 security security").
        base = focus if focus != service_name else self.config.sources.get('_default_queries', {}).get(service, service_name)
        queries = list(dict.fromkeys([
            base,
            base if service_name in base.lower() else f'{base} {service_name}',
            f'{base} report analysis',
        ]))
        # Broad searches can return only inaccessible pages from one publisher.
        # Reserve a query for an independently hosted, already-allowlisted source.
        docs = self.config.services[service].get('docs', [])
        targeted={}
        if docs:
            # Replace the third near-duplicate broad query, keeping requests
            # bounded while reserving independent publisher searches.
            queries=queries[:2]
            for domain in dict.fromkeys([docs[0],docs[-1]]):
                query=f'{base} site:{domain}'
                queries.append(query)
                targeted[query]=domain.split('/')[0]
        entry = {'service':service, 'query':queries[0], 'queries':queries, 'domain_allowlist':domains,
                 'urls':[], 'status':'searched'}
        query_log.append(entry)  # log even if failed or zero results
        if self.mode == 'demo':
            fixture = self.fixture.get(service)
            if fixture is None:
                entry['status']='no_fixture'; raise SourceError('No offline fixture for requested service')
            entry['status']='fixture_not_live'
            entry['urls']=[{'url':fixture['url'],'status':'offline_fixture_not_fetched'}]
            return [dict(fixture, id='fixture:'+service, kind='fixture', service=service, fetched=False)]
        if not self.api_key:
            entry['status']='no_search_api'; raise SourceError('TAVILY_API_KEY missing; live marketing research cannot use KB as fallback')
        candidates=[]
        seen_urls=set()
        search_errors=[]
        for query in queries:
            # Tavily filters by host only. Path-limited entries (github.com/ethereum) are enforced by host_allowed().
            hosts = sorted({d.split('/')[0] for d in domains if '/' not in d}) or sorted({d.split('/')[0] for d in domains})
            if query in targeted:
                hosts=[targeted[query]]
            payload={'api_key':self.api_key,'query':query,'search_depth':'basic',
                     'max_results':5,'include_domains':hosts,'include_answer':False}
            if format == 'news-reaction': payload['time_range']='month'
            request_log={'query':query,'include_domains':hosts,'status':'requested','out_of_scope_urls':[]}
            entry.setdefault('requests',[]).append(request_log)
            try:
                if self._gate is None: self._gate=asyncio.Semaphore(2)     # at most 2 searches at once across all slots
                async with self._gate:
                    response=await self._post(payload)
                for item in response.json().get('results',[]):
                    url=item.get('url','')
                    if query in targeted and not host_allowed(url,hosts,self.config.sources['_deny']):
                        request_log['out_of_scope_urls'].append(url)
                        continue
                    if url and url not in seen_urls:
                        seen_urls.add(url); candidates.append(item)
                request_log['status']='completed'
            except Exception as exc:
                request_log['status']='failed'
                search_errors.append(type(exc).__name__)
                entry.setdefault('search_error_detail',[]).append(self._explain(exc))
                continue
        if not candidates and search_errors:
            entry['status']='search_error:' + search_errors[-1]
            raise SourceError('Live search failed (' + entry['search_error_detail'][-1] + '); this slot must fail without KB fallback') from None
        verified=[]
        entry['urls']=[{'url':x.get('url',''),'status':'not_processed_capacity'} for x in candidates]
        # Read the most relevant candidate first, rather than trusting Tavily's
        # initial rank. Log all search results, including rejected candidates.
        indexed=sorted(enumerate(candidates), key=lambda pair:
                       (relevant_result(pair[1],service,focus),
                        (urlsplit(pair[1].get('url','')).hostname or '').lower()!='github.com'),
                       reverse=True)
        for index,item in indexed:
            url=item.get('url','')
            log=entry['urls'][index]
            if not host_allowed(url,domains,self.config.sources['_deny']):
                log['status']='blocked_by_domain_policy'; continue
            if noise_url(url,self.config.sources.get('_deny_url_patterns')):
                log['status']='noise_url'; continue
            if urlsplit(url).path.lower().endswith('.pdf'):
                log['status']='unsupported_pdf'; continue      # text only: a PDF cannot be quoted exactly
            if not relevant_result(item,service,focus):
                log['status']='unrelated_to_topic'; continue
            try:
                page=await fetch_url(url,domains,self.config.sources['_deny'],self.client)
                if not body_relevant(page,service,focus):
                    log['status']='body_unrelated_to_topic'; continue
                verified.append({'id':f'web:{service}:{len(verified)+1}', 'url':url,
                                 'title':item.get('title','')[:160], 'body':page,
                                 'service':service,'fetched':True,'kind':'web'})
                log['status']='fetched'
            except (httpx.HTTPError, SourceError) as exc:
                # Keep the reason (HTTP 403, PDF, too short...). The old log said only "SourceError" for every cisa.gov page.
                log['status']='fetch_failed:' + type(exc).__name__
                log['reason']=str(exc)[:120] if isinstance(exc,SourceError) else type(exc).__name__
            if len(verified)>=3: break
        entry['status']='ok' if verified else 'zero_verified_sources'
        if not verified:
            counts={}
            for item in entry['urls']:
                status=item['status']
                counts[status]=counts.get(status,0)+1
            details=', '.join(f'{status}={count}' for status,count in sorted(counts.items())) or 'no search results'
            raise SourceError('No fetched, allowlisted web pages for this marketing slot ('+details+')')
        return verified


def fixture_data():
    """Non-current text explicitly reserved for offline demo and tests."""
    return {
      'web3-security':{'title':'Fixture: smart contract audit coverage',
        'url':'https://docs.soliditylang.org/en/latest/security-considerations.html',
        'body':'An audit focuses on what the code does within the defined scope. Upgrade controls and operational security still need separate review. This fixture is a static teaching example, not newly fetched news.'},
      'pentesting':{'title':'Fixture: supply chain security', 'url':'https://owasp.org/www-project-top-ten/',
        'body':'A web application depends on build pipelines, credentials, packages and deployment settings. Reviewing only on-chain logic leaves those routes unchecked. This is an offline teaching fixture, not live research.'},
      'agentic-security':{'title':'Fixture: agent permission boundaries',
        'url':'https://genai.owasp.org/',
        'body':'An AI agent with access to tools needs explicit policies for what actions it can perform. Untrusted instructions can appear in documents and tool responses. This content is an offline teaching fixture.'},
      'agentic-payments':{'title':'Fixture: payment authorization',
        'url':'https://docs.cdp.coinbase.com/',
        'body':'A payment-capable agent needs bounded spending permissions and a trusted payment integration. Authentication does not replace transaction policy. This is offline demonstration text.'}
    }
