"""Three independent import commands plus demo, report, and raw evidence audit."""
import argparse,json
from pathlib import Path
from . import __file__ as module_file
from .core import connect,import_file,report,audit
from .policy import SUBSYSTEMS

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('command',choices=[x+'-import' for x in SUBSYSTEMS]+['demo','report','audit','init']);p.add_argument('--db',default='state.sqlite');p.add_argument('--file');a=p.parse_args(argv)
    db=connect(a.db)
    if a.command.endswith('-import'):
        if not a.file:p.error('--file required')
        result=import_file(db,a.command[:-7],a.file)
    elif a.command=='demo':
        root=Path(__file__).resolve().parents[1]/'fixtures';steps=[]
        for phase in ('ok','bad'):
            for sub in SUBSYSTEMS:
                result=import_file(db,sub,root/f'{sub}-{phase}.json')
                steps.append({'fixture':f'{sub}-{phase}.json',**result,'decisions':len(report(db)['decisions'])})
        result={'steps':steps,'report':report(db),'audit':audit(db),'imported_module':str(Path(module_file).resolve())}
    elif a.command=='report':result=report(db)
    elif a.command=='audit':result=audit(db)
    else:result={'db':str(Path(a.db).resolve()),'initialized':True}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if a.command!='audit' or result['ok'] else 2
