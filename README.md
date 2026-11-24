# 翻译交付术语争议采证台

完全离线的源文定界、术语合议和译稿验收结算三账采证与重裁演示。仅使用合成 JSON 和本地 SQLite。原始文件 SHA-256 留存，幂等重放，异内容事件 ID 冲突回滚，保留历史裁定及旧新值转移。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m termsettle demo --db demo.sqlite
python -m termsettle audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：`source-import`、`term-import`、`delivery-import`，均使用 `--db` 和 `--file`。`fixtures/` 包含每项正常及异常证据。三条跨账路径：源文变更增加应译章节、取消旧结算；术语撤回冻结关联译段并重算金额；独立审校指出争议反向冻结术语版及交付。`report` 输出业务实体、当前决定、历史、源文件摘要和每次状态转移；`audit` 从原证重建核验。

## 共享汇款逐票核销与预收对账

`fixtures/shared-remittance.json` 中客户 C 一笔 1000 元 `bank-1` 的附言同时提到 INV-A(600)、INV-B(400)。附言票号只是匹配线索（`remittance_capture.received_for` 保留该匹配），**不是分配授权**；只有客户正式签署的付款指令才能驱动 `receivables` 逐票清算，同一笔银行入账在“逐票核销 + 项目预收 + 待核未分配 + 已退回”中的份额合计恒等于实收净额，杜绝把 1000 元从每张票重复扣成 -400/-600。

```bash
python -B -m termsettle.receivables     # 打印 仅凭附言待核 / 指令一(600+400) / 指令二(600+400预收) 三口径
python -B -m pytest -q -p no:cacheprovider
```

两条正式指令互斥可选：`instr-full` 分配 INV-A 600、INV-B 400，两票余额与预收均为 0；`instr-advance` 清 INV-A 600、余 400 作 `ADV-C-NEXTP2` 下期项目预收，INV-B 仍应收 400。账本（`receivables.build_ledger` / `ReceivablesLedger`）另支持部分与连续核销、超额及银行净额重复占用拒绝、贷项冲抵与作废票拒收新款、签署主体/付款账户/票号-开票主体对应校验、预收转票双向链，以及银行退回时按实际 allocation/advance 份额逐条反向冲销并保留原流水（另开 `kind=return` 流水）。`reconciliation()` 输出银行净实收、票份额、预收余额、待核及 `difference`/`balanced`，并可传入外部对账单金额计算 `statement_difference`。
