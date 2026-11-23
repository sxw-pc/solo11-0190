import json,tempfile,unittest
from pathlib import Path
from termsettle.core import connect,import_file,report,audit
from termsettle.policy import calculate

ROOT=Path(__file__).resolve().parents[1]/'fixtures'
def result(db,subject,name):
    return db.execute('SELECT result FROM current_decisions WHERE subject=? AND decision=?',(subject,name)).fetchone()[0]
class TranslationWorkflow(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'test.sqlite')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def load(self,sub,phase='ok'):return import_file(self.db,sub,ROOT/f'{sub}-{phase}.json')
    def baseline(self):
        for sub in ('source','term','delivery'):self.load(sub)
    def test_ten_decisions_and_replay(self):
        self.baseline();self.assertEqual(10,len(report(self.db)['decisions']))
        self.assertEqual('200',result(self.db,'J1','迟到源文修订是否重算结算金额'))
        self.assertEqual(0,self.load('delivery')['inserted']);self.assertTrue(audit(self.db)['ok'])
    def test_source_change_changes_coverage_and_payment(self):
        self.baseline();self.load('source','bad')
        self.assertEqual('漏章',result(self.db,'J1','章节覆盖是否支持交付验收'))
        self.assertEqual('0',result(self.db,'J1','迟到源文修订是否重算结算金额'))
    def test_term_withdrawal_changes_delivery_payment(self):
        self.baseline();self.load('term','bad')
        self.assertEqual('撤回重审',result(self.db,'API:safety','术语撤回是否保留旧译稿依据'))
        self.assertEqual('0',result(self.db,'J1','迟到源文修订是否重算结算金额'))
    def test_review_failure_blocks_billing(self):
        self.baseline();self.load('delivery','bad')
        self.assertEqual('返工',result(self.db,'J1','独立审校意见是否触发返工'))
        self.assertEqual('冻结',result(self.db,'API:safety','争议术语是否冻结关联译段'))
        self.assertEqual('0',result(self.db,'J1','迟到源文修订是否重算结算金额'))
    def test_collision_rolls_back(self):
        self.baseline();before=report(self.db)
        event=json.loads((ROOT/'source-ok.json').read_text(encoding='utf-8'))[0];event['data']['rate']=99
        p=Path(self.tmp.name)/'collision.json';p.write_text(json.dumps([event]),encoding='utf-8')
        with self.assertRaises(ValueError):import_file(self.db,'source',p)
        self.assertEqual(before,report(self.db))
    def test_independent_rule_boundaries(self):
        import copy
        base=sum((json.loads((ROOT/f'{s}-ok.json').read_text(encoding='utf-8')) for s in ('source','term','delivery')),[])
        def decision(rows,subject,name):return next(d[2] for d in calculate(rows)[1] if d[0]==subject and d[1]==name)
        rows=copy.deepcopy(base);rows[1]['data']['signer']='Stranger'
        self.assertEqual('越权',decision(rows,'J1','源文签发主体能否授权翻译'))
        rows=copy.deepcopy(base);rows.append(dict(rows[1],id='conflict',data=dict(signer='Client',chapter='ch1',hash='h2')))
        self.assertEqual('冻结',decision(rows,'J1','同版源文冲突是否冻结章节'))
        rows=copy.deepcopy(base);rows.append(dict(rows[3],id='duplicate-vote'))
        self.assertEqual('去重',decision(rows,'API:safety','同人重复投票是否影响定版'))
        rows=copy.deepcopy(base);rows[5]['data']['chapter']='unapproved'
        self.assertEqual('漏章',decision(rows,'J1','章节覆盖是否支持交付验收'))
if __name__=='__main__':unittest.main()
