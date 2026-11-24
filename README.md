# 翻译交付术语争议采证台

完全离线的源文定界、术语合议和译稿验收结算三账采证与重裁演示。仅使用合成 JSON 和本地 SQLite。原始文件 SHA-256 留存，幂等重放，异内容事件 ID 冲突回滚，保留历史裁定及旧新值转移。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m termsettle demo --db demo.sqlite
python -m termsettle audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：`source-import`、`term-import`、`delivery-import`，均使用 `--db` 和 `--file`。`fixtures/` 包含每项正常及异常证据。三条跨账路径：源文变更增加应译章节、取消旧结算；术语撤回冻结关联译段并重算金额；独立审校指出争议反向冻结术语版及交付。`report` 输出业务实体、当前决定、历史、源文件摘要和每次状态转移；`audit` 从原证重建核验。

## 首响 SLA 复核（T-47）

`tickets` 证据链在 `termsettle/ticket_capture.py`（渠道归并、实际送达、正文内容、签认暂停窗口）与 `termsettle/sla_decision.py`（计时、达标裁定、可审计证据、迟到回执修订台账）。`fixtures/service-thread.json` 保留 T-47 原始 0/2/45 三事件，并附两路合同、跨渠道重投、错误投递重试、暂停边界与迟到网关回执回归。

```python
import json
from termsettle.sla_decision import decide, adjudicate_all_contracts, SLALedger

data = json.load(open("fixtures/service-thread.json", encoding="utf-8"))
decide(data, contract="appendix-a-auto-diagnostic")  # elapsed=2,  met=True （附件A 认可自动诊断）
decide(data, contract="human-substantive-zh")        # elapsed=45, met=False, overdue_minutes=15
```

裁定只由“归并后的同一争议会话 + 经客户侧回执证实的送达 + 正文/术语匹配 + 合同资格 + 有效暂停区间”驱动，事件 `kind` 不作为白名单。`SLALedger.add_receipt(...)` 仅追加迟到网关回执并生成新修订，`rulings` 保留旧裁定，`raw_log_unchanged()` 核验原始事件日志未被改写。

```bash
python -B -m pytest -q -p no:cacheprovider
```
