"""共享银行汇款 + 客户正式分配指令的逐票核销/预收/退回逆转测试。

运行：``python -B -m pytest -q -p no:cacheprovider``

覆盖任务硬约束：
* 两条正式签署指令（600+400 清两票 / 600 清 A、400 转下期预收）；
* 只有附言票号不构成授权，余额留待核，绝不按票面自动摊光；
* 部分核销、连续核销、超额/超银行净额拒绝（同一现金不得重复使用）；
* 贷项冲抵（部分冲减 + 全额贷项结清票拒收新款）与作废票拒收；
* 签署主体/付款账户/票号与开票主体的对应校验；
* 银行退回按真实占用份额反向冲销（含预收及预收转票链），原流水保留；
* 预收转票双向链与客户预收余额、对账差异。
原有翻译结算夹具由 tests/test_workflow.py 继续覆盖，整套 pytest 一并运行。
"""

import json
import unittest
from pathlib import Path

import pytest

from termsettle.remittance_capture import (
    AllocationError,
    received_for,
)
from termsettle.receivables import (
    FIXTURE_PATH,
    balances,
    build_ledger,
    reconcile,
    settle,
)

DATA = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
A, B = "INV-A", "INV-B"
BANK = "bank-1"
CLIENT = "Client-C"
PAYER_ACCOUNT = "ACCT-CLIENT-C-CMB-6222"
ISSUER_ACCOUNT = "ACCT-ISSUER-BOC-0001"


@pytest.fixture()
def data():
    # 深拷贝，避免任何用例通过嵌套 dict 污染共享夹具
    return json.loads(json.dumps(DATA))


def _instruction(inst_id, allocations=(), advances=(), **over):
    inst = {
        "id": inst_id,
        "bank_ref": BANK,
        "customer": CLIENT,
        "signed_by": "Client",
        "payer_account": PAYER_ACCOUNT,
        "at": "2026-11-21",
        "allocations": [dict(a) for a in allocations],
        "advances": [dict(a) for a in advances],
    }
    inst.update(over)
    return inst


# ------------------------------------------------------------ 夹具与旧入口

def test_fixture_core_facts_preserved():
    """任务给定的核心事实保持不变：两票 600/400，同一笔 bank-1=1000 附言提及两票。"""
    assert [(i["id"], i["amount"]) for i in DATA["invoices"]] == [(A, 600), (B, 400)]
    bank = next(r for r in DATA["remittances"] if r["id"] == BANK)
    assert bank["amount"] == 1000
    assert bank["invoice_refs"] == [A, B]
    assert {i["id"] for i in DATA["instructions"]} == {"instr-full", "instr-advance"}


def test_received_for_is_only_narration_match():
    # 旧入口仍可按附言找到 bank-1，但这只是匹配线索，不是核销授权
    assert len(received_for(DATA["remittances"], A)) == 1
    assert received_for(DATA["remittances"], A)[0]["id"] == BANK
    assert len(received_for(DATA["remittances"], B)) == 1


# ------------------------------------------------------------ 无授权：留待核

def test_narration_only_leaves_everything_pending(data):
    ledger = build_ledger(data)  # 不选择任何正式指令
    # 不得再出现 -400/-600 的重复核销
    assert ledger.balances() == {A: 600, B: 400}
    assert ledger.received_totals() == {A: 0, B: 0}
    assert ledger.customer_advance_balance(CLIENT) == 0
    assert ledger.unallocated_balance(CLIENT) == 1000
    positions = ledger.customer_positions()[CLIENT]
    assert positions["pending_verification_balance"] == 1000
    assert positions["advance_balance"] == 0
    assert positions["unapplied_balance"] == 1000
    rec = ledger.reconciliation()
    assert rec["bank_net_total"] == 1000
    assert rec["difference"] == 0
    assert rec["balanced"] is True
    row = rec["bank_rows"][0]
    assert row["unallocated_shares"] == 1000
    assert row["invoice_shares"] == 0 and row["advance_shares"] == 0


def test_legacy_balances_call_does_not_double_apply(data):
    # 旧函数签名 balances(invoices, remittances) 仍可调用；
    # 只有 invoice_refs 附言时，正确结论是全额应收 + 来款待核，而非负数。
    assert balances(data["invoices"], data["remittances"]) == {A: 600, B: 400}
    # 直接传单夹具 dict 也可
    assert balances(data) == {A: 600, B: 400}


# ------------------------------------------------------------ 指令一：两票清

def test_instruction_one_allocates_600_400_and_zeroes_both(data):
    ledger = build_ledger(data, instruction="instr-full")
    assert ledger.balances() == {A: 0, B: 0}
    assert ledger.received_totals() == {A: 600, B: 400}
    assert ledger.customer_advance_balance(CLIENT) == 0
    assert ledger.unallocated_balance(CLIENT) == 0

    st_a = ledger.invoice_statement(A)
    assert st_a["status"] == "paid" and st_a["balance"] == 0
    assert st_a["allocation_ids"] and len(st_a["allocation_ids"]) == 1

    # 银行份额守恒：600 + 400 恰好等于实收净额 1000
    row = ledger.reconciliation()["bank_rows"][0]
    assert row["invoice_shares"] == 1000
    assert row["advance_shares"] == 0
    assert row["unallocated_shares"] == 0
    assert row["difference"] == 0 and row["balanced"]

    # 追溯链：两条核销都同时挂着 bank-1 与 instr-full
    trace = ledger.trace(BANK)
    assert {a["invoice_id"]: a["amount"] for a in trace["allocations"]} == {A: 600, B: 400}
    assert all(a["instruction_id"] == "instr-full" for a in trace["allocations"])
    assert [i["id"] for i in trace["instructions"]] == ["instr-full"]


# ------------------------------------------------------------ 指令二：A 清 + 400 预收

def test_instruction_two_keeps_b_and_books_advance(data):
    ledger = build_ledger(data, instruction="instr-advance")
    assert ledger.balances() == {A: 0, B: 400}          # INV-B 仍应收 400
    assert ledger.received_totals() == {A: 600, B: 0}
    assert ledger.customer_advance_balance(CLIENT) == 400
    assert ledger.advance_balances() == {CLIENT: 400}
    assert ledger.unallocated_balance(CLIENT) == 0

    advs = ledger.advance_positions(CLIENT)
    assert len(advs) == 1
    adv = advs[0]
    assert adv["id"] == "ADV-C-NEXTP2"
    assert adv["project"] == "下期项目 P2 翻译预收"
    assert adv["remaining"] == 400 and adv["conversion_ids"] == []

    row = ledger.reconciliation()["bank_rows"][0]
    assert row["invoice_shares"] == 600
    assert row["advance_shares"] == 400
    assert row["unallocated_shares"] == 0
    assert row["difference"] == 0


def test_two_instructions_are_alternative_routes_over_same_cash(data):
    first = build_ledger(data, instruction="instr-full")
    second = build_ledger(data, instruction="instr-advance")

    # 银行现金完全相同
    for led in (first, second):
        bank = led.bank_flows[BANK]
        assert bank["amount"] == 1000 and bank["id"] == BANK

    # 应收/预收结论不同
    assert first.balances() == {A: 0, B: 0}
    assert first.customer_advance_balance(CLIENT) == 0
    assert second.balances() == {A: 0, B: 400}
    assert second.customer_advance_balance(CLIENT) == 400

    # 每条路线内部都守恒，且同一 bank-1 没有被重复使用
    assert first.reconciliation()["balanced"]
    assert second.reconciliation()["balanced"]
    for led in (first, second):
        assert sum(r["invoice_shares"] + r["advance_shares"]
                   + r["unallocated_shares"]
                   for r in led.reconciliation()["bank_rows"]) == 1000


# ------------------------------------------------------------ 部分核销

def test_partial_writeoff_leaves_balance_and_pending(data):
    ledger = build_ledger(data)
    ledger.apply_instruction(_instruction(
        "instr-partial", [{"invoice": A, "amount": 300}]))

    assert ledger.balances()[A] == 300
    assert ledger.balances()[B] == 400
    assert ledger.received_totals()[A] == 300
    # 银行里还有 700 未获分配授权 -> 待核，而不是自动摊给 B
    assert ledger.unallocated_balance() == 700
    assert ledger.invoice_statement(A)["status"] == "partial"
    assert ledger.reconciliation()["balanced"]


def test_sequential_instructions_clear_invoice(data):
    ledger = build_ledger(data)
    ledger.apply_instruction(_instruction("p1", [{"invoice": A, "amount": 200}]))
    assert ledger.balances()[A] == 400
    ledger.apply_instruction(_instruction("p2", [{"invoice": A, "amount": 400}]))
    assert ledger.balances()[A] == 0
    # bank-1 已占用 600，剩余 400 待核，恰好还能清 B
    assert ledger.unallocated_balance() == 400
    ledger.apply_instruction(_instruction("p3", [{"invoice": B, "amount": 400}]))
    assert ledger.balances() == {A: 0, B: 0}
    assert ledger.unallocated_balance() == 0
    assert ledger.reconciliation()["balanced"]


def test_over_allocation_to_invoice_is_rejected_atomically(data):
    ledger = build_ledger(data)
    # INV-A 只有 600，却要核 700
    bad = _instruction("over-invoice", [{"invoice": A, "amount": 700}])
    with pytest.raises(AllocationError) as exc:
        ledger.apply_instruction(bad)
    assert any("超过可收余额" in e for e in exc.value.errors)
    # 原子失败：没有任何核销落账，银行份额仍全部待核
    assert ledger.balances() == {A: 600, B: 400}
    assert ledger.allocations == []
    assert ledger.unallocated_balance() == 1000


def test_bank_cash_cannot_be_committed_twice(data):
    ledger = build_ledger(data, instruction="instr-full")
    # instr-full 已用满 bank-1 的 1000；任何新分配都不得再占用同一笔现金
    extra = _instruction("double-spend", [{"invoice": A, "amount": 1}])
    result = ledger.submit_instruction(extra)
    assert result["ok"] is False
    assert any("剩余可分配净额" in e for e in result["errors"])
    assert ledger.balances() == {A: 0, B: 0}
    assert ledger.reconciliation()["balanced"]


# ------------------------------------------------------------ 贷项冲抵 / 作废

def test_partial_credit_note_reduces_receivable(data):
    ledger = build_ledger(data)
    ledger.add_invoice("INV-C", 500, customer=CLIENT, issuer="ISSUER-TS",
                       issuer_account=ISSUER_ACCOUNT)
    ledger.add_credit_note("INV-C", 100, "CN-C-1", reason="质量折让")
    # 贷项后应收 400，收 400 即清
    assert ledger.invoice_statement("INV-C")["remaining"] == 400
    ledger.add_bank_entry("bank-2", 400, payer=CLIENT,
                          payer_account=PAYER_ACCOUNT, account=ISSUER_ACCOUNT)
    ledger.apply_instruction(_instruction(
        "ic", [{"invoice": "INV-C", "amount": 400}], bank_ref="bank-2"))
    st = ledger.invoice_statement("INV-C")
    assert st["status"] == "paid" and st["balance"] == 0
    assert st["credit_note_total"] == 100 and st["received"] == 400


def test_fully_credited_invoice_cannot_absorb_new_remittance(data):
    ledger = build_ledger(data)
    ledger.add_invoice("INV-D", 200, customer=CLIENT, issuer="ISSUER-TS",
                       issuer_account=ISSUER_ACCOUNT)
    ledger.add_credit_note("INV-D", 200, "CN-D-FULL")
    assert ledger.invoice_statement("INV-D")["status"] == "credited"

    ledger.add_bank_entry("bank-3", 200, payer=CLIENT,
                          payer_account=PAYER_ACCOUNT, account=ISSUER_ACCOUNT)
    result = ledger.submit_instruction(_instruction(
        "hit-credited", [{"invoice": "INV-D", "amount": 200}], bank_ref="bank-3"))
    assert result["ok"] is False
    assert any("贷项" in e for e in result["errors"])
    # 被拒收后这笔 bank-3 仍完整待核，没有被发票吸收
    assert ledger.received_totals()["INV-D"] == 0
    assert ledger.unallocated_balance() == 1200  # bank-1 1000 + bank-3 200
    assert ledger.reconciliation()["balanced"]


def test_void_invoice_cannot_absorb_new_remittance(data):
    ledger = build_ledger(data)
    ledger.add_invoice("INV-E", 300, customer=CLIENT, void=True)
    ledger.add_bank_entry("bank-4", 300, payer=CLIENT,
                          payer_account=PAYER_ACCOUNT, account=ISSUER_ACCOUNT)
    result = ledger.submit_instruction(_instruction(
        "hit-void", [{"invoice": "INV-E", "amount": 300}], bank_ref="bank-4"))
    assert result["ok"] is False
    assert any("作废" in e for e in result["errors"])
    assert ledger.invoice_statement("INV-E")["status"] == "void"
    assert ledger.balances()["INV-E"] == 0  # 作废票不挂应收


# ------------------------------------------------------------ 主体/账户/票对应

def test_signer_must_match_authorized_signing_subject(data):
    ledger = build_ledger(data)
    result = ledger.submit_instruction(_instruction(
        "bad-signer", [{"invoice": A, "amount": 600}], signed_by="Stranger"))
    assert result["ok"] is False
    assert any("授权签署" in e for e in result["errors"])
    assert ledger.allocations == []


def test_payer_account_must_match_actual_inflow(data):
    ledger = build_ledger(data)
    result = ledger.submit_instruction(_instruction(
        "bad-account", [{"invoice": A, "amount": 600}],
        payer_account="ACCT-UNKNOWN-9999"))
    assert result["ok"] is False
    assert any("付款账户" in e for e in result["errors"])


def test_invoice_customer_must_match_instruction(data):
    other = build_ledger(data)
    other.add_customer("Client-Z", authorized_signers=["Z"],
                       payer_accounts=["ACCT-Z"])
    other.add_invoice("INV-Z", 100, customer="Client-Z")
    result = other.submit_instruction(_instruction(
        "cross-customer", [{"invoice": "INV-Z", "amount": 100}]))
    assert result["ok"] is False
    assert any("不对应" in e for e in result["errors"])


def test_narration_refs_are_not_auto_authorization(data):
    # 即使附言同时写了 A、B，也不允许无 signed_by 的“指令”自动摊票
    ledger = build_ledger(data)
    unsigned = _instruction("unsigned", [{"invoice": A, "amount": 600},
                                         {"invoice": B, "amount": 400}])
    unsigned["signed_by"] = None
    result = ledger.submit_instruction(unsigned)
    assert result["ok"] is False
    assert ledger.balances() == {A: 600, B: 400}


# ------------------------------------------------------------ 银行退回逆转

def test_bank_return_reverses_each_invoice_share_and_keeps_original_flow(data):
    ledger = build_ledger(data, instruction="instr-full")
    reversal = ledger.reverse_bank_entry(BANK, reason="客户汇错，银行退回")

    # 冲销记录点名两条原核销，而不是只减一个总额
    assert set(reversal["allocation_ids"]) == {"AL-0001", "AL-0002"}
    assert reversal["allocation_total"] == 1000
    assert reversal["total"] == 1000

    # 两票应收恢复
    assert ledger.balances() == {A: 600, B: 400}
    assert ledger.received_totals() == {A: 0, B: 0}

    # 原流水保留（金额、编号不变），另开反向流水并双向链接
    original = ledger.bank_flows[BANK]
    assert original["amount"] == 1000 and original["kind"] == "credit"
    assert original["status"] == "reversed" and original["reversed_by"] == reversal["id"]
    return_flow = ledger.bank_flows[reversal["return_flow_id"]]
    assert return_flow["kind"] == "return"
    assert return_flow["amount"] == 1000
    assert return_flow["reverses"] == BANK

    # 原核销记录保留但置 reversed，并回链冲销单
    assert all(a["status"] == "reversed" for a in ledger.allocations)
    assert all(a["reversed_by"] == reversal["id"] for a in ledger.allocations)

    rec = ledger.reconciliation()
    assert rec["bank_net_total"] == 0
    assert rec["difference"] == 0 and rec["balanced"]

    # 已退回的 bank-1 不能再吸收新指令
    again = ledger.submit_instruction(_instruction(
        "after-return", [{"invoice": A, "amount": 1}]))
    assert again["ok"] is False


def test_bank_return_reverses_invoice_share_and_advance(data):
    ledger = build_ledger(data, instruction="instr-advance")
    reversal = ledger.reverse_bank_entry(BANK, reason="银行拒付/退回")

    # 实际占用 = INV-A 600 + 预收 400，INV-B 从未被触碰
    assert reversal["allocation_total"] == 600
    assert reversal["advance_remaining_total"] == 400
    assert reversal["total"] == 1000
    assert reversal["advance_ids"] == ["ADV-C-NEXTP2"]

    assert ledger.balances() == {A: 600, B: 400}
    assert ledger.customer_advance_balance(CLIENT) == 0
    adv = ledger.advance_positions()[0]
    assert adv["status"] == "reversed" and adv["remaining"] == 0
    assert ledger.reconciliation()["balanced"]

    # 冲销后全量报告仍可 JSON 序列化，且客户头寸不冒出账户号之类的幽灵主体
    report = ledger.summary()
    json.dumps(report, ensure_ascii=False)
    assert set(report["customer_positions"]) == {CLIENT}
    assert report["customer_positions"][CLIENT]["unapplied_balance"] == 0


# ------------------------------------------------------------ 预收转票双向链

def test_advance_to_invoice_has_bidirectional_chain(data):
    ledger = build_ledger(data, instruction="instr-advance")
    ledger.add_invoice("INV-F", 400, customer=CLIENT, issuer="ISSUER-TS",
                       issuer_account=ISSUER_ACCOUNT)

    allocation = ledger.convert_advance("ADV-C-NEXTP2", "INV-F")
    # 双向链：核销指向预收，预收的 conversion_ids 指回核销
    assert allocation["advance_id"] == "ADV-C-NEXTP2"
    adv = ledger.advance_position("ADV-C-NEXTP2")
    assert allocation["id"] in adv["conversion_ids"]
    assert adv["remaining"] == 0 and adv["converted"] == 400

    assert ledger.balances()["INV-F"] == 0
    assert ledger.customer_advance_balance(CLIENT) == 0
    assert ledger.reconciliation()["balanced"]

    # 从任一端都能追到另一端
    trace_adv = ledger.trace("ADV-C-NEXTP2")
    assert trace_adv["conversions"][0]["invoice_id"] == "INV-F"
    trace_inv = ledger.trace("INV-F")
    assert trace_inv["allocations"][0]["advance_id"] == "ADV-C-NEXTP2"


def test_bank_return_unwinds_advance_already_converted_to_invoice(data):
    ledger = build_ledger(data, instruction="instr-advance")
    ledger.add_invoice("INV-F", 400, customer=CLIENT)
    ledger.convert_advance("ADV-C-NEXTP2", "INV-F")
    assert ledger.balances()["INV-F"] == 0

    # bank-1 退回必须沿双向链撤销已转票的 400，而不是只在总额上减 1000
    reversal = ledger.reverse_bank_entry(BANK)
    assert set(reversal["allocation_ids"]) == {"AL-0001", "AL-0002"}
    assert reversal["allocation_total"] == 1000
    assert reversal["advance_remaining_total"] == 0
    assert reversal["total"] == 1000

    assert ledger.balances() == {A: 600, B: 400, "INV-F": 400}
    assert ledger.customer_advance_balance(CLIENT) == 0
    assert ledger.reconciliation()["bank_net_total"] == 0
    assert ledger.reconciliation()["balanced"]


def test_partial_advance_conversion_and_reversal(data):
    ledger = build_ledger(data, instruction="instr-advance")
    ledger.add_invoice("INV-F", 400, customer=CLIENT)
    ledger.convert_advance("ADV-C-NEXTP2", "INV-F", 150)
    assert ledger.balances()["INV-F"] == 250
    assert ledger.customer_advance_balance(CLIENT) == 250

    reversal = ledger.reverse_bank_entry(BANK)
    # 600 直接核销 + 150 已结转核销 = 750 票份额，另加 250 预收余额冲回
    assert reversal["allocation_total"] == 750
    assert reversal["advance_remaining_total"] == 250
    assert reversal["total"] == 1000
    assert ledger.balances()["INV-F"] == 400
    assert ledger.reconciliation()["balanced"]


# ------------------------------------------------------------ 对账差异输出

def test_reconciliation_reports_external_statement_gap(data):
    ledger = build_ledger(data, instruction="instr-full")
    # 内部账始终守恒
    assert ledger.reconciliation()["difference"] == 0
    # 与银行对外对账单相比少 1 元（例如手续费/入账差异）-> 明确给出对账差异
    rec = ledger.reconciliation(external_statement={"bank-1": 999})
    assert rec["statement_difference"] == 1
    assert rec["balanced"] is False
    assert rec["bank_rows"][0]["statement_difference"] == 1

    matched = ledger.reconciliation(external_statement=1000)
    assert matched["statement_difference"] == 0
    assert matched["balanced"] is True


def test_settle_summary_is_json_ready_and_reports_key_figures():
    summary = settle(instruction="instr-advance")
    json.dumps(summary, ensure_ascii=False)  # 必须可序列化
    assert summary["balances"] == {A: 0, B: 400}
    assert summary["customer_advances"] == {CLIENT: 400}
    assert summary["reconciliation_difference"] == 0
    assert summary["balanced"] is True
    assert reconcile(DATA, instruction="instr-full")["balanced"] is True


def test_conservation_identity_holds_for_every_bank_row(data):
    # 随机混合操作后，逐行恒等式必须成立：
    # net = invoice_shares + advance_shares + unallocated_shares
    ledger = build_ledger(data)
    ledger.apply_instruction(_instruction("p1", [{"invoice": A, "amount": 250}]))
    ledger.apply_instruction(_instruction(
        "p2", [{"invoice": B, "amount": 200}],
        advances=[{"ref": "ADV-X", "project": "预存", "amount": 50}]))
    for row in ledger.reconciliation()["bank_rows"]:
        # 净实收 = 逐票份额 + 预收余额份额 + 待核份额
        assert row["net_received"] == (
            row["invoice_shares"] + row["advance_shares"]
            + row["unallocated_shares"])
        # 原入账总额 = 上述份额 + 已退回
        assert row["gross_received"] == row["net_received"] + row["returned"]
        assert row["difference"] == 0
    assert ledger.balances() == {A: 350, B: 200}


class ReceivablesSeedTest(unittest.TestCase):
    """保留种子测试类名/方法名；旧断言 -400/-600 记录的是重复核销缺陷。"""

    def test_shared_transfer_double_applied(self):
        data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        # 附言仍能匹配到同一笔 bank-1
        self.assertEqual(len(received_for(data["remittances"], "INV-A")), 1)
        # 仅凭附言不再把整笔 1000 从每张票扣一次：两票保留全额应收
        self.assertEqual(balances(data["invoices"], data["remittances"]),
                         {"INV-A": 600, "INV-B": 400})
        # 正式指令一落地后两票结清，而不是 -400/-600
        ledger = build_ledger(data, instruction="instr-full")
        self.assertEqual(ledger.balances(), {"INV-A": 0, "INV-B": 0})
        self.assertTrue(ledger.reconciliation()["balanced"])


if __name__ == "__main__":
    unittest.main()
