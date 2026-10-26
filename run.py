"""Single CLI for the weekly plan or one content piece."""
import argparse
import asyncio
import json
from datetime import datetime,timezone
from pathlib import Path
from sanad.config import ROOT, Config, Plan, Slot
from sanad.agents import run_week
from sanad.providers import LiveProvider, DemoProvider, runtime_options
from sanad.research import WebResearch, fixture_data


def main():
    options=runtime_options(ROOT/'.env')
    config=Config(ROOT)
    parser=argparse.ArgumentParser(description='SanadSquad content studio')
    parser.add_argument('--mode',choices=['demo','live'],default='demo',help='Demo uses explicitly labeled offline fixtures')
    parser.add_argument('--concurrency',type=int,default=2)
    parser.add_argument('--type',choices=list(config.post_types),help='Generate just one piece')
    parser.add_argument('--topic',default='')
    parser.add_argument('--service',default='web3-security')
    parser.add_argument('--format',default='service-overview')
    parser.add_argument('--source-mode',choices=['kb','external'],default='kb')
    parser.add_argument('--source-url',default='')
    parser.add_argument('--set',action='append',default=[],metavar='SLOT[.FIELD]=VALUE',help='Change a slot focus or service/format/source fields')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--reuse',type=Path,help='Earlier run folder: keep its approved pieces and re-run only the others')
    args=parser.parse_args()
    if args.type:
        is_marketing=config.post_types[args.type]['source_policy']=='marketing'
        slot=Slot(id='one_piece',type=args.type,focus=args.topic,
                  service=args.service if is_marketing else '',
                  format=args.format,source_mode='web' if is_marketing else args.source_mode,
                  source_url=args.source_url)
        plan=Plan(slots=[slot])
    else:
        plan=config.plan.model_copy(deep=True)
        updates=[s.model_dump() for s in plan.slots]
        editable={'focus','service','format','source_mode','source_url','vulnerability_kb_id'}
        for edit in args.set:
            if '=' not in edit: parser.error('--set needs SLOT=TOPIC or SLOT.FIELD=VALUE')
            selector,value=edit.split('=',1)
            name,field=(selector.split('.',1)+['focus'])[:2] if '.' in selector else (selector,'focus')
            selected=next((s for s in updates if s['id']==name),None)
            if selected is None: parser.error('Unknown plan slot: '+name)
            if field not in editable: parser.error('Unsupported slot field: '+field)
            selected[field]=value
        try: plan=Plan.model_validate({'slots':updates})
        except ValueError as exc: parser.error(str(exc))
    config.validate_plan(plan)
    provider=DemoProvider() if args.mode=='demo' else LiveProvider(options=options)
    import os
    web=WebResearch(config,args.mode,options.get('TAVILY_API_KEY'),fixture_data() if args.mode=='demo' else None)
    out=args.output or ROOT/'output'/datetime.now(timezone.utc).strftime('run-%Y%m%d-%H%M%S-%f')
    summary=asyncio.run(run_week(plan,config,provider,web,out,args.concurrency,reuse=args.reuse))
    print(json.dumps(summary,indent=2))
    print('Output:',out.resolve())
    return 0 if summary['pieces_not_shipped']==0 else 1


if __name__=='__main__':raise SystemExit(main())
