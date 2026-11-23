# 翻译交付术语争议采证台

完全离线的源文定界、术语合议和译稿验收结算三账采证与重裁演示。仅使用合成 JSON 和本地 SQLite。原始文件 SHA-256 留存，幂等重放，异内容事件 ID 冲突回滚，保留历史裁定及旧新值转移。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m termsettle demo --db demo.sqlite
python -m termsettle audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：`source-import`、`term-import`、`delivery-import`，均使用 `--db` 和 `--file`。`fixtures/` 包含每项正常及异常证据。三条跨账路径：源文变更增加应译章节、取消旧结算；术语撤回冻结关联译段并重算金额；独立审校指出争议反向冻结术语版及交付。`report` 输出业务实体、当前决定、历史、源文件摘要和每次状态转移；`audit` 从原证重建核验。
