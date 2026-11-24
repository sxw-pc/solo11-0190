"""逐票清算、客户预收与对账差异的只读投影。

资金事实（银行流水 / 签署指令 / 核销 / 预收 / 退回）由
:mod:`termsettle.remittance_capture` 捕获并保证守恒；本模块把这些事实投影成
财务要的三张结论：

* 每张翻译服务发票的实收、贷项冲抵与应收余额（不再重算可计费译段）；
* 每个客户的正式预收余额、待核未分配来款；
* 银行实收净额与“逐票核销 + 预收余额 + 待核”之间的对账差异（恒为 0 才平）。

另保留旧函数 ``balances(invoices, remittances)`` 的调用形态：在只有附言匹配、
没有签署分配指令时，不会把整笔共享汇款从每张票面重复扣掉，而是保留全额应收，
等待指令分配。
"""

import json
from pathlib import Path

try:  # 支持 `python -m termsettle.receivables` 与包内导入
    from .remittance_capture import (
        AllocationError,
        RemittanceBook,
        from_cents,
        to_cents,
    )
except ImportError:  # 兼容直接 `python termsettle/receivables.py`
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from termsettle.remittance_capture import (  # noqa: E402
        AllocationError,
        RemittanceBook,
        from_cents,
        to_cents,
    )

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "shared-remittance.json"

__all__ = [
    "ReceivablesLedger", "Ledger", "Receivables",
    "load_fixture", "load_data", "build_ledger", "settle",
    "balances", "invoice_balances", "received_totals",
    "advance_balances", "customer_advance_balance",
    "unallocated_balances", "reconcile",
    "FIXTURE_PATH",
]


def load_fixture(path=None):
    path = Path(path) if path else FIXTURE_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def load_data(source=None):
    """None -> 内置共享汇款夹具；路径/JSON 字符串/dict 均可。"""
    if source is None:
        return load_fixture()
    if isinstance(source, (bytes, bytearray)):
        return json.loads(bytes(source).decode("utf-8"))
    if isinstance(source, (str, Path)):
        text = str(source)
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return json.loads(text)
        return json.loads(Path(text).read_text(encoding="utf-8"))
    return source


class ReceivablesLedger(RemittanceBook):
    """在捕获账之上提供逐票应收 / 预收 / 对账投影。"""

    def __init__(self, *args, require_signature=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.require_signature = require_signature

    # ------------------------------------------------------------ 客户归并
    def _cid(self, ref):
        rec = self._resolve_customer(ref) if ref is not None else None
        return rec["id"] if rec is not None else ref

    # ------------------------------------------------------------ 逐票清算
    def invoice_statement(self, invoice_id):
        inv = self.invoices[invoice_id]
        gross_c = inv["amount_cents"]
        credit_c = self.credit_total_cents(invoice_id)
        received_c = self.received_cents(invoice_id)
        remaining_c = gross_c - credit_c - received_c
        closed, close_reason = self.invoice_is_closed(invoice_id)

        if inv.get("void"):
            status = "void"
        elif gross_c - credit_c <= 0:
            status = "credited"
        elif remaining_c <= 0:
            status = "paid"
        elif received_c > 0:
            status = "partial"
        else:
            status = "open"

        return {
            "id": invoice_id,
            "customer": self._cid(inv.get("customer")),
            "issuer": inv.get("issuer"),
            "currency": inv.get("currency", self.currency),
            "amount": from_cents(gross_c),
            "credit_note_total": from_cents(credit_c),
            "received": from_cents(received_c),
            "remaining": from_cents(max(remaining_c, 0)),
            # 作废票在应收控制账上余额为 0，原始票面 remaining 仍保留可查
            "balance": 0 if inv.get("void") else from_cents(max(remaining_c, 0)),
            "status": status,
            "collectible": not closed and remaining_c > 0,
            "closed_reason": close_reason,
            "allocation_ids": [a["id"] for a in self._active_allocations(invoice_id)],
            "credit_note_ids": [c["id"] for c in self.credit_notes
                                if c["invoice_id"] == invoice_id and c["status"] == "active"],
        }

    def invoice_statements(self):
        return [self.invoice_statement(iid) for iid in self.invoices]

    def balances(self):
        """两张票（及账内全部票）的应收余额；共享汇款绝不会产生重复扣票负数。"""
        return {iid: self.invoice_statement(iid)["balance"]
                for iid in self.invoices}

    def balance(self, invoice_id):
        return self.invoice_statement(invoice_id)["balance"]

    def received_for_invoice(self, invoice_id):
        return self.invoice_statement(invoice_id)["received"]

    def receivable_balances(self):
        return self.balances()

    def received_totals(self):
        """每票实际核销实收（含预收转票）。"""
        return {iid: self.invoice_statement(iid)["received"]
                for iid in self.invoices}

    # ------------------------------------------------------------ 预收 / 待核
    def advance_position(self, advance_id):
        adv = self.advances[advance_id]
        remaining_c = self.advance_remaining_cents(advance_id) \
            if adv["status"] == "active" else 0
        converted_c = sum(a["amount_cents"] for a in self.allocations
                          if a.get("advance_id") == advance_id
                          and a["status"] == "active")
        return {
            "id": adv["id"],
            "ref": adv.get("ref"),
            "project": adv.get("project"),
            "customer": self._cid(adv.get("customer")),
            "bank_id": adv["bank_id"],
            "instruction_id": adv["instruction_id"],
            "amount": adv["amount"],
            "converted": from_cents(converted_c),
            "remaining": from_cents(remaining_c),
            "status": adv["status"],
            "conversion_ids": list(adv["conversion_ids"]),
            "reversed_by": adv.get("reversed_by"),
        }

    def advance_positions(self, customer=None):
        cid = self._cid(customer) if customer is not None else None
        rows = [self.advance_position(aid) for aid in self.advances]
        if cid is not None:
            rows = [r for r in rows if r["customer"] == cid]
        return rows

    def customer_advance_balance(self, customer):
        cid = self._cid(customer)
        return from_cents(sum(
            self.advance_remaining_cents(aid)
            for aid, adv in self.advances.items()
            if adv["status"] == "active" and self._cid(adv.get("customer")) == cid
        ))

    def advance_balances(self):
        out = {}
        for aid, adv in self.advances.items():
            if adv["status"] != "active":
                continue
            cid = self._cid(adv.get("customer"))
            out[cid] = out.get(cid, 0) + self.advance_remaining_cents(aid)
        return {k: from_cents(v) for k, v in out.items()}

    def pending_by_bank(self):
        """每笔银行入账“待核未分配”的可追溯份额（只有附言、无授权的钱在这）。"""
        rows = []
        for bid, bank in self.bank_flows.items():
            if bank["kind"] != "credit":
                continue
            pending_c = self.bank_pending_cents(bid)
            if pending_c < 0:
                # 守恒被破坏时如实暴露，而不是吞掉
                pending_c = pending_c
            rows.append({
                "bank_id": bid,
                "customer": self._cid(bank.get("payer")),
                "amount": from_cents(max(pending_c, 0)),
                "difference": from_cents(pending_c) if pending_c < 0 else 0,
                "status": "pending_verification" if pending_c > 0 else "allocated",
                "narration_invoice_refs": list(bank.get("invoice_refs", [])),
            })
        return rows

    def unallocated_balance(self, customer=None):
        cid = self._cid(customer) if customer is not None else None
        total_c = 0
        for row in self.pending_by_bank():
            if cid is None or row["customer"] == cid:
                # difference<0 时 pending 行 amount 被截成 0，这里直接重算分
                bank = self.bank_flows[row["bank_id"]]
                pending_c = self.bank_pending_cents(row["bank_id"])
                total_c += max(pending_c, 0)
        return from_cents(total_c)

    def customer_positions(self):
        # 只取正式/裸档案的规范 id（别名键与规范键指向同一 record["id"]）
        ids = {rec["id"] for rec in self.customers.values()
               if isinstance(rec, dict) and "id" in rec}
        ids.update(self._cid(b.get("payer")) for b in self.bank_flows.values())
        ids.update(self._cid(i.get("customer")) for i in self.invoices.values())
        ids.discard(None)
        positions = {}
        for cid in sorted(i for i in ids if i is not None):
            adv_c = sum(
                self.advance_remaining_cents(aid)
                for aid, adv in self.advances.items()
                if adv["status"] == "active" and self._cid(adv.get("customer")) == cid
            )
            pending_c = sum(
                max(self.bank_pending_cents(bid), 0)
                for bid, bank in self.bank_flows.items()
                if bank["kind"] == "credit"
                and self._cid(bank.get("payer")) == cid
            )
            positions[cid] = {
                "advance_balance": from_cents(adv_c),
                "pending_verification_balance": from_cents(pending_c),
                # 客户来款中尚未落到发票上的全部负债/待核余额
                "unapplied_balance": from_cents(adv_c + pending_c),
            }
        return positions

    # ------------------------------------------------------------ 对账
    def bank_conservation_rows(self):
        rows = []
        for bid, bank in self.bank_flows.items():
            if bank["kind"] != "credit":
                continue
            amount_c = bank["amount_cents"]
            returned_c = self.returned_total_cents(bid)
            net_c = amount_c - returned_c
            invoice_c = sum(a["amount_cents"] for a in self.allocations
                            if a["bank_id"] == bid and a["status"] == "active")
            advance_remaining_c = sum(
                self.advance_remaining_cents(aid)
                for aid, adv in self.advances.items()
                if adv["bank_id"] == bid and adv["status"] == "active"
            )
            pending_c = self.bank_pending_cents(bid)
            accounted_c = invoice_c + advance_remaining_c + max(pending_c, 0)
            diff_c = net_c - invoice_c - advance_remaining_c - pending_c
            rows.append({
                "bank_id": bid,
                "gross_received": from_cents(amount_c),
                "returned": from_cents(returned_c),
                "net_received": from_cents(net_c),
                "invoice_shares": from_cents(invoice_c),
                "advance_shares": from_cents(advance_remaining_c),
                "unallocated_shares": from_cents(max(pending_c, 0)),
                "accounted_total": from_cents(accounted_c),
                "difference": from_cents(diff_c),
                "balanced": diff_c == 0,
            })
        return rows

    def reconciliation(self, external_statement=None):
        rows = self.bank_conservation_rows()
        bank_in_c = sum(b["amount_cents"] for b in self.bank_flows.values()
                        if b["kind"] == "credit")
        returned_c = sum(f["amount_cents"] for f in self.bank_flows.values()
                         if f["kind"] in ("return", "debit_reversal", "chargeback"))
        invoice_c = sum(a["amount_cents"] for a in self.allocations
                        if a["status"] == "active")
        advance_c = sum(
            self.advance_remaining_cents(aid)
            for aid, adv in self.advances.items() if adv["status"] == "active"
        )
        pending_c = sum(max(self.bank_pending_cents(bid), 0)
                        for bid, b in self.bank_flows.items() if b["kind"] == "credit")
        # 内部守恒差异用未截断的 pending 计算，异常时能露出负数/缺口
        internal_diff_c = (bank_in_c - returned_c - invoice_c - advance_c
                           - sum(self.bank_pending_cents(bid)
                                 for bid, b in self.bank_flows.items()
                                 if b["kind"] == "credit"))

        result = {
            "currency": self.currency,
            "bank_received_total": from_cents(bank_in_c),
            "bank_returned_total": from_cents(returned_c),
            "bank_net_total": from_cents(bank_in_c - returned_c),
            "invoice_received_total": from_cents(invoice_c),
            "advance_balance_total": from_cents(advance_c),
            "pending_verification_total": from_cents(pending_c),
            "accounted_total": from_cents(invoice_c + advance_c + pending_c),
            "difference": from_cents(internal_diff_c),
            "balanced": internal_diff_c == 0,
            "bank_rows": rows,
        }

        if external_statement is not None:
            if isinstance(external_statement, dict):
                ext_total_c = 0
                for row in rows:
                    if row["bank_id"] in external_statement:
                        expected_c = to_cents(external_statement[row["bank_id"]])
                        actual_c = to_cents(row["net_received"])
                        row["statement_amount"] = from_cents(expected_c)
                        row["statement_difference"] = from_cents(actual_c - expected_c)
                        row["statement_matches"] = actual_c == expected_c
                        ext_total_c += expected_c
                result["external_statement_total"] = from_cents(ext_total_c)
                result["statement_difference"] = from_cents(
                    bank_in_c - returned_c - ext_total_c)
            else:
                ext_c = to_cents(external_statement)
                result["external_statement_total"] = from_cents(ext_c)
                result["statement_difference"] = from_cents(
                    bank_in_c - returned_c - ext_c)
            result["balanced"] = (
                internal_diff_c == 0 and result["statement_difference"] == 0)
        return result

    def reconcile(self, external_statement=None):
        return self.reconciliation(external_statement)

    # ------------------------------------------------------------ 双向链追踪
    def trace(self, ref):
        """按 bank / instruction / invoice / advance 任意编号输出完整证据链。"""
        if ref in self.bank_flows:
            bank = self.bank_flows[ref]
            return {
                "bank_flow": {k: v for k, v in bank.items()},
                "instructions": [dict(v) for v in self.instructions.values()
                                 if v.get("bank_ref") == ref],
                "allocations": [dict(a) for a in self.allocations
                                if a["bank_id"] == ref],
                "advances": [self.advance_position(aid)
                             for aid, a in self.advances.items()
                             if a["bank_id"] == ref],
                "reversals": [dict(r) for r in self.reversals
                              if r["bank_id"] == ref],
            }
        if ref in self.invoices:
            return {"invoice": self.invoice_statement(ref),
                    "allocations": [dict(a) for a in self.allocations
                                    if a["invoice_id"] == ref],
                    "credit_notes": [dict(c) for c in self.credit_notes
                                     if c["invoice_id"] == ref]}
        if ref in self.advances:
            return {"advance": self.advance_position(ref),
                    "bank_flow": dict(self.bank_flows[self.advances[ref]["bank_id"]]),
                    "conversions": [dict(a) for a in self.allocations
                                     if a.get("advance_id") == ref]}
        if ref in self.instructions:
            return {"instruction": dict(self.instructions[ref])}
        raise AllocationError(f"trace: 未知编号 {ref!r}")

    # ------------------------------------------------------------ 全量输出
    def summary(self):
        recon = self.reconciliation()
        return {
            "currency": self.currency,
            "balances": self.balances(),
            "receivable_balances": self.balances(),
            "received": self.received_totals(),
            "invoice_received": self.received_totals(),
            "invoices": self.invoice_statements(),
            "customer_positions": self.customer_positions(),
            "customer_advances": self.advance_balances(),
            "advance_balances": self.advance_balances(),
            "unallocated": self.pending_by_bank(),
            "unallocated_balance": self.unallocated_balance(),
            "bank_flows": [dict(b) for b in self.bank_flows.values()],
            "instructions": [dict(i) for i in self.instructions.values()],
            "allocations": [dict(a) for a in self.allocations],
            "advances": [self.advance_position(aid) for aid in self.advances],
            "credit_notes": [dict(c) for c in self.credit_notes],
            "reversals": [dict(r) for r in self.reversals],
            "events": [dict(e) for e in self.events],
            "reconciliation": recon,
            "reconciliation_difference": recon["difference"],
            "balanced": recon["balanced"],
        }


Ledger = ReceivablesLedger
Receivables = ReceivablesLedger


# ---------------------------------------------------------------- 从夹具构造

def build_ledger(source=None, *, instruction=None, instructions=None,
                 apply_all_instructions=False, require_signature=True,
                 ledger=None):
    """从夹具/dict 构造账本并按选择落地指令。

    instruction:  指令 id 或指令 dict，只走一条路线（两条正式指令互斥演示）。
    instructions: 多条指令的 id/dict/列表，按顺序落地（支持部分核销、二次分配）。
    apply_all_instructions=True: 落地夹具内全部指令（注意两条互斥路线不要同时选）。
    """
    data = load_data(source)
    book = ledger or ReceivablesLedger(
        currency=data.get("currency", "CNY"),
        require_signature=require_signature)
    book.require_signature = require_signature

    for c in data.get("customers", []) or []:
        book.add_customer(
            c["id"], name=c.get("name"),
            authorized_signers=c.get("authorized_signers") or c.get("signers"),
            payer_accounts=c.get("payer_accounts"),
            aliases=c.get("aliases"))
    for i in data.get("issuers", []) or data.get("billing_entities", []) or []:
        book.add_issuer(i["id"], legal_name=i.get("legal_name") or i.get("name"),
                        receivable_accounts=i.get("receivable_accounts")
                                           or i.get("accounts"))

    for inv in data.get("invoices", []) or []:
        book.add_invoice(
            inv["id"], inv["amount"],
            customer=inv.get("customer") or inv.get("customer_id"),
            issuer=inv.get("issuer") or inv.get("issuer_id"),
            issuer_account=inv.get("issuer_account") or inv.get("receivable_account"),
            currency=inv.get("currency"),
            void=inv.get("void", False),
            status=inv.get("status"),
            **{k: v for k, v in inv.items()
               if k not in {"id", "amount", "customer", "customer_id", "issuer",
                            "issuer_id", "issuer_account", "receivable_account",
                            "currency", "void", "status"}})
        book.ensure_customer(inv.get("customer") or inv.get("customer_id"))

    bank_items = (data.get("bank_transactions") or data.get("bank_flows")
                  or data.get("remittances") or [])
    for item in bank_items:
        extra = {k: v for k, v in item.items() if k not in {"id", "amount"}}
        book.add_bank_entry(item.get("id"), item.get("amount"), **extra)
        book.ensure_customer(item.get("payer") or item.get("customer"))

    for cn in data.get("credit_notes", []) or data.get("credit_memos", []) or []:
        book.add_credit_note(
            cn.get("invoice_id") or cn.get("invoice"), cn["amount"],
            credit_note_id=cn.get("id"), reason=cn.get("reason"), at=cn.get("at"))

    data_instructions = (data.get("instructions")
                         or data.get("payment_instructions") or [])
    by_id = {i.get("id"): i for i in data_instructions}

    selected = []
    if instructions is not None:
        seq = instructions
        if isinstance(seq, (str, dict)):
            seq = [seq]
        for ref in seq:
            selected.append(by_id[ref] if isinstance(ref, str) else ref)
    if instruction is not None:
        one = by_id[instruction] if isinstance(instruction, str) else instruction
        selected.append(one)
    if apply_all_instructions and not selected:
        selected = list(data_instructions)

    for inst in selected:
        book.apply_instruction(inst)
    return book


def settle(source=None, *, instruction=None, **kwargs):
    return build_ledger(source, instruction=instruction, **kwargs).summary()


# ---------------------------------------------------------------- 向后兼容入口

def _embedded_instruction(rem):
    """旧形态汇款 dict 若自带 allocations/applications，视为已授权分配。"""
    allocs = (rem.get("allocations") or rem.get("applications")
              or rem.get("splits") or rem.get("invoice_allocations")
              or rem.get("distribution"))
    if not allocs:
        return None

    def inv_ref(a):
        return a.get("invoice") or a.get("invoice_id") or a.get("invoice_ref") \
            or a.get("ref") or a.get("id")

    return {
        "id": f"instr-embedded-{rem.get('id')}",
        "bank_ref": rem.get("id"),
        "customer": rem.get("customer") or rem.get("payer"),
        "signed_by": rem.get("signed_by") or rem.get("signer"),
        "payer_account": rem.get("payer_account"),
        "at": rem.get("at") or rem.get("booked_at"),
        "allocations": [{"invoice": inv_ref(a),
                         "amount": a.get("amount") or a.get("applied")}
                        for a in allocs],
        "advances": rem.get("advances") or rem.get("prepayments") or [],
    }


def balances(invoices=None, remittances=None, instructions=None, *,
             data=None, **kwargs):
    """发票应收余额。

    兼容三种调用：
      * ``balances(ledger)``
      * ``balances(fixture_dict, instruction='instr-full')``
      * 旧形态 ``balances(invoices, remittances[, instructions])``

    旧形态下，若汇款只有 ``invoice_refs`` 附言而无签署指令/内嵌 allocations，
    不会重复扣减，各票保留全额应收（来款挂待核）。
    """
    if isinstance(invoices, RemittanceBook):
        return invoices.balances()

    if isinstance(invoices, (str, Path)) and remittances is None:
        return build_ledger(invoices,
                            instruction=kwargs.get("instruction"),
                            instructions=instructions).balances()

    if data is not None:
        return build_ledger(data, instruction=kwargs.get("instruction"),
                            **{k: v for k, v in kwargs.items()
                               if k == "instructions"}).balances()

    if isinstance(invoices, dict) and remittances is None:
        return build_ledger(invoices,
                            instruction=kwargs.get("instruction"),
                            instructions=instructions).balances()

    source = {
        "invoices": invoices or [],
        "remittances": remittances or [],
        "instructions": list(instructions or []),
    }
    embedded = [_embedded_instruction(r) for r in (remittances or [])]
    source["instructions"].extend(i for i in embedded if i is not None)
    # 没有任何正式主体档案时，函数式旧入口放宽签署形式要求，
    # 但金额守恒、发票状态、超额与账户一致性仍然生效。
    ledger = build_ledger(source,
                          apply_all_instructions=True,
                          require_signature=False)
    return ledger.balances()


def invoice_balances(*args, **kwargs):
    return balances(*args, **kwargs)


def received_totals(ledger):  # noqa: F811 - 模块级便捷函数
    if isinstance(ledger, ReceivablesLedger):
        return ledger.received_totals()
    return build_ledger(ledger).received_totals()


def advance_balances(ledger):  # noqa: F811
    return ledger.advance_balances() if isinstance(ledger, ReceivablesLedger) \
        else build_ledger(ledger).advance_balances()


def customer_advance_balance(ledger, customer):  # noqa: F811
    return ledger.customer_advance_balance(customer)


def unallocated_balances(ledger):
    return {row["bank_id"]: row["amount"] for row in ledger.pending_by_bank()} \
        if isinstance(ledger, ReceivablesLedger) \
        else build_ledger(ledger).unallocated_balance()


def reconcile(ledger_or_source=None, external_statement=None, **kwargs):
    book = ledger_or_source if isinstance(ledger_or_source, ReceivablesLedger) \
        else build_ledger(ledger_or_source, **kwargs)
    return book.reconciliation(external_statement)


def _brief(summary):
    rec = summary["reconciliation"]
    return {
        "invoice_received": summary["invoice_received"],
        "receivable_balances": summary["receivable_balances"],
        "customer_advances": summary["customer_advances"],
        "unallocated_balance": summary["unallocated_balance"],
        "reconciliation_difference": rec["difference"],
        "balanced": rec["balanced"],
    }


def main(argv=None):
    """离线打印共享汇款三种口径：仅凭附言待核 / 指令一 / 指令二。"""
    import argparse
    p = argparse.ArgumentParser(description="共享汇款逐票核销与预收对账")
    p.add_argument("--fixture", default=str(FIXTURE_PATH))
    p.add_argument("--instruction", default=None,
                   help="只输出指定指令 id；缺省输出待核 + 两条路线")
    a = p.parse_args(argv)
    data = load_fixture(a.fixture) if Path(a.fixture).exists() else load_data(a.fixture)
    if a.instruction:
        report = settle(data, instruction=a.instruction)
        print(json.dumps(_brief(report), ensure_ascii=False, indent=2))
        return 0
    report = {
        "narration_only_pending": _brief(settle(data)),
        "route_1_instr_full_600_400": _brief(settle(data, instruction="instr-full")),
        "route_2_instr_advance_600_then_400":
            _brief(settle(data, instruction="instr-advance")),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
