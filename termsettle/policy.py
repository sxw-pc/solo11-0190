"""Source authorization, terminology consensus and billable delivery."""
from collections import defaultdict
SUBSYSTEMS=('source','term','delivery')

def _groups(events,sub):
    result=defaultdict(list)
    for e in events:
        if e['subsystem']==sub:result[e['subject']].append(e)
    return result

def _dec(subject,name,result,rows):
    return subject,name,result,'按源文、术语与交付原证交叉重算',[e['id'] for e in rows]

def sources(events):
    states={};entities=[];decisions=[]
    for job,rows in _groups(events,'source').items():
        contracts=[e for e in rows if e['action']=='contract'];chapters=[e for e in rows if e['action']=='chapter'];changes=[e for e in rows if e['action']=='change']
        contract=contracts[-1] if contracts else None
        authorized=bool(contract and all(e['data']['signer'] in contract['data']['signers'] and contract['data']['from']<=e['at']<=contract['data']['to'] for e in chapters))
        pairs=defaultdict(set)
        for e in chapters:pairs[(e['data']['chapter'],e['version'])].add(e['data']['hash'])
        conflict=any(len(v)>1 for v in pairs.values())
        approved=all(e['data']['approved_by'] in (contract['data']['signers'] if contract else []) for e in changes)
        required=set(contract['data']['chapters']) if contract else set()
        if approved:
            for e in changes:
                if e['data']['kind']=='add':required.add(e['data']['chapter'])
                elif e['data']['kind']=='remove':required.discard(e['data']['chapter'])
        states[job]={'authorized':authorized and not conflict,'required':required,'change_ok':approved,'rate':contract['data']['rate'] if contract else 0,'changed':bool(changes)}
        entities.extend(('委托源文定界','委托合同' if e['action'] in ('contract','change') else '源文章节',e['id'],e['action'],e['id']) for e in rows)
        for name,result in [('源文签发主体能否授权翻译','授权' if authorized else '越权'),('同版源文冲突是否冻结章节','冻结' if conflict else '一致'),('增删章节是否形成收费变更','有效变更' if approved and changes else '无有效变更')]:decisions.append(_dec(job,name,result,rows))
    return states,entities,decisions

def terms(events):
    states={};entities=[];decisions=[]
    for key,rows in _groups(events,'term').items():
        proposals=[e for e in rows if e['action']=='proposal'];votes=[e for e in rows if e['action']=='vote'];withdrawals=[e for e in rows if e['action']=='withdraw']
        proposal=proposals[-1] if proposals else None
        unique={e['data']['reviewer'] for e in votes if e['data']['reviewer']!=e['data'].get('translator')}
        duplicate=len(votes)>len(unique)
        conflict=len({e['data']['translation'] for e in proposals if e['data']['context']==(proposal['data']['context'] if proposal else '')})>1
        independent_dispute=any(e['subsystem']=='delivery' and e['action']=='review' and e['data'].get('disputed_term')==key and e['data']['reviewer']!=e['data']['translator'] and not e['data']['approved'] for e in events)
        withdrawn=bool(withdrawals)
        valid=bool(proposal and len(unique)>=2 and not conflict and not withdrawn and not independent_dispute)
        states[key]={'valid':valid,'context':proposal['data']['context'] if proposal else None,'translation':proposal['data']['translation'] if proposal else None,'withdrawn':withdrawn}
        entities.extend(('术语合议定版','术语提案' if e['action'] in ('proposal','withdraw') else '裁定选票',e['id'],e['action'],e['id']) for e in rows)
        linked=rows+[e for e in events if e['subsystem']=='delivery' and e['action']=='review' and e['data'].get('disputed_term')==key]
        for name,result in [('术语上下文是否匹配译段并形成可核验裁定','定版' if valid else '待议'),('同人重复投票是否影响定版','去重' if duplicate else '独立'),('争议术语是否冻结关联译段','冻结' if conflict or independent_dispute else '无争议'),('术语撤回是否保留旧译稿依据','撤回重审' if withdrawn else '保持')]:decisions.append(_dec(key,name,result,linked))
    return states,entities,decisions

def deliveries(events,source,term):
    entities=[];decisions=[]
    for job,rows in _groups(events,'delivery').items():
        segments=[e for e in rows if e['action']=='segment'];reviews=[e for e in rows if e['action']=='review']
        required=source.get(job,{}).get('required',set());covered=bool(required and required.issubset({e['data']['chapter'] for e in segments}))
        term_ok=all(term.get(e['data']['term_key'],{}).get('valid') and term[e['data']['term_key']]['context']==e['data']['context'] for e in segments)
        independent=bool(reviews and all(e['data']['reviewer']!=e['data']['translator'] for e in reviews))
        accepted=bool(source.get(job,{}).get('authorized') and covered and term_ok and independent and all(e['data']['approved'] for e in reviews))
        billable=sum(e['data']['words'] for e in segments if e['data']['chapter'] in required and term.get(e['data']['term_key'],{}).get('valid')) if accepted else 0
        amount=billable*source.get(job,{}).get('rate',0)
        entities.extend(('译稿验收结算','译稿段落' if e['action']=='segment' else '验收结算',e['id'],e['action'],e['id']) for e in rows)
        linked=rows+[e for e in events if e['subsystem']=='source' and e['subject']==job]+[e for e in events if e['subsystem']=='term' and e['subject'] in {s['data']['term_key'] for s in segments}]
        for name,result in [('章节覆盖是否支持交付验收','覆盖' if covered else '漏章'),('独立审校意见是否触发返工','独立通过' if independent and accepted else '返工'),('迟到源文修订是否重算结算金额',str(amount))]:decisions.append(_dec(job,name,result,linked))
    return entities,decisions

def calculate(events):
    s,se,sd=sources(events);t,te,td=terms(events);de,dd=deliveries(events,s,t)
    return se+te+de,sd+td+dd
