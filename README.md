# 翻译交付术语争议采证台

完全离线的源文定界、术语合议和译稿验收结算三账采证与重裁演示。仅使用合成 JSON 和本地 SQLite。原始文件 SHA-256 留存，幂等重放，异内容事件 ID 冲突回滚，保留历史裁定及旧新值转移。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m termsettle demo --db demo.sqlite
python -m termsettle audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：`source-import`、`term-import`、`delivery-import`，均使用 `--db` 和 `--file`。`fixtures/` 包含每项正常及异常证据。三条跨账路径：源文变更增加应译章节、取消旧结算；术语撤回冻结关联译段并重算金额；独立审校指出争议反向冻结术语版及交付。`report` 输出业务实体、当前决定、历史、源文件摘要和每次状态转移；`audit` 从原证重建核验。

## 审校会谈录音采样覆盖账（`audio_capture` / `meeting_archive`）

主产物是录音的**采样覆盖账和可复听证书**，不裁决术语译法或稿费。覆盖坐标是同一设备的原始采样时轴：文件数、压缩字节哈希、解码哈希、文件头、音频时长与 SQLite 事务都不作覆盖依据。

- 转码/重采样副本（如 PCM→AAC 的 a3）凭转码链映射回源片区间，只入 `duplicates`，永远不填补源片之外的缺口；重采样比例由两侧采样率推出，映射与声明不符即标记 `provenance_mismatch`。
- 独立录音设备的可信原件只作辅助音轨（`备录原件` 等级）；设备身份以会话清单登记的封存指纹核验，冒名主麦者隔离入 `spoofs`。
- 静音是有效原始样本；真正丢采给出精确可复核缺段与来源等级；另采录采样率变化、双声道同步、封存回执（含迟到回执，只修订对应会议）。
- 两条授权恢复：路径 A 以主麦奇偶冗余块＋签名采样索引重构验证，签发全段`完整`（`冗余重构`等级）；路径 B 以房间备录可信原件签发`组合`证书，主麦段与辅助段各保等级，剩余缺段精确留存、要求主麦补采，备录永不转写为主麦原件。
- 旧的计数式错误“完整”报告按 revision 0 留痕，新证书以 `supersedes` 链接取代。

```powershell
python -B -m pytest -q -p no:cacheprovider
```

