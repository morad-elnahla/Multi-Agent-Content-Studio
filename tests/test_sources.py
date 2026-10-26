import asyncio
import pytest
import httpx
from sanad.config import ROOT,Config
from sanad.research import WebResearch, SourceError, fixture_data


def test_zero_search_results_are_logged():
    cfg=Config(ROOT)
    class FakeClient:
        async def post(self,*args,**kwargs):
            return httpx.Response(200,json={'results':[]},request=httpx.Request('POST',args[0]))
    research=WebResearch(cfg,'live',api_key='test',client=FakeClient())
    log=[]
    with pytest.raises(SourceError):
        asyncio.run(research.search('web3-security','oracle security',log))
    assert len(log)==1 and log[0]['status']=='zero_verified_sources'
    assert log[0]['query']=='oracle security'


def test_blocked_search_result_is_audited():
    cfg=Config(ROOT)
    class FakeClient:
        async def post(self,*args,**kwargs):
            return httpx.Response(200,json={'results':[{'url':'https://evil.example/','title':'evil'}]},request=httpx.Request('POST',args[0]))
    research=WebResearch(cfg,'live',api_key='test',client=FakeClient())
    log=[]
    with pytest.raises(SourceError):
        asyncio.run(research.search('web3-security','risk',log))
    assert log[0]['urls'][0]['status']=='blocked_by_domain_policy'


def test_live_web_source_must_be_fetched_before_use():
    from contextlib import asynccontextmanager
    cfg=Config(ROOT)
    allowed='https://genai.owasp.org/a-new-report'
    class FakeClient:
        async def post(self,*args,**kwargs):
            return httpx.Response(200,json={'results':[{'url':allowed,'title':'Trust boundaries'},
                {'url':'https://evil.example.com/not-allowed','title':'Ignore'}]},
                request=httpx.Request('POST',args[0]))
        @asynccontextmanager
        async def stream(self,method,url,**kwargs):
            assert url==allowed
            yield httpx.Response(200,text='<html><body>'+'A concrete investigation of prompt injection and tool abuse in an llm agent. '*5+'</body></html>',
                  headers={'content-type':'text/html'},request=httpx.Request(method,url))
    r=WebResearch(cfg,'live',api_key='test',client=FakeClient())
    log=[]
    data=asyncio.run(r.search('agentic-security','agent security',log,'news-reaction'))
    assert len(data)==1 and data[0]['fetched'] and data[0]['url']==allowed
    assert log[0]['urls'][0]['status']=='fetched'
    assert log[0]['urls'][1]['status']=='blocked_by_domain_policy'
    assert log[0]['status']=='ok'
