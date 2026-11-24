"""银行流水捕获、客户正式付款指令校验与资金份额账。

本模块只负责三件事：

1. 登记真实银行流水（原始入账永久保留，退回/拒付另开反向流水，不删改原账）；
2. 接收并校验客户正式签署的核销指令（签署主体、付款账户、票号与开票主体对应）；
3. 把每笔银行入账拆成互斥且可追溯的资金份额：逐票核销 / 项目预收 / 待核未分配 /
   银行退回。任何时刻同一 bank 流水的有效份额合计不得超过其实收净额，
   从根上杜绝“同一笔 1000 元在 INV-A、INV-B 各扣一次”的重复核销。

只有附言（invoice_refs / narration）而没有正式分配指令时，票号仅作匹配线索，
资金进入“待核未分配”，绝不按票面自动摊光。
"""

from collections import OrderedDict
from decimal import Decimal, ROUND_HALF_UP

__all__ = [
    "RemittanceError", "ValidationError", "AllocationError",
    "to_cents", "from_cents", "received_for",
    "RemittanceBook", "RemittanceCapture",
    "capture_bank_entry", "validate_instruction", "allocate",
    "reverse_bank_entry",
]


# ---------------------------------------------------------------- 金额工具

class RemittanceError(ValueError):
    """汇款/核销账基础错误。"""


class ValidationError(RemittanceError):
    """指令身份或要素校验失败。"""


class AllocationError(RemittanceError):
    """分配违反守恒、发票状态或额度约束。

    可能一次携带多条错误，便于财务一次性退回补正。
    """

    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = [str(e) for e in errors]
        super().__init__("; ".join(self.errors))


def to_cents(value):
    """把元金额转成整数分，杜绝浮点误差。"""
    if isinstance(value, bool):  # bool 是 int 子类，排除掉
        raise AllocationError(f"非法金额: {value!r}")
    if isinstance(value, int):
        return value * 100
    try:
        dec = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise AllocationError(f"非法金额: {value!r}") from exc
    if not dec.is_finite():
        raise AllocationError(f"非法金额: {value!r}")
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(cents):
    """整数分转回元；整数元返回 int，保证 0 == 0 而不是 0.00 之类的意外。"""
    cents = int(cents)
    if cents % 100 == 0:
        return cents // 100
    return round(cents / 100, 2)


def _amount(record, key="amount_cents"):
    return from_cents(record[key])


# ---------------------------------------------------------------- 附言匹配（保留旧入口）

def received_for(remittances, invoice):
    """旧入口：按附言票号列出提及该票的银行流水。

    注意：这只是“附言匹配线索”，不代表该流水已被授权核销到本票。
    真正的实收份额必须来自客户签署指令生成的 allocation 记录。
    """
    return [item for item in remittances if invoice in item.get("invoice_refs", [])]


# ---------------------------------------------------------------- 账本

class RemittanceBook:
    """银行流水 + 付款指令 + 逐票核销/预收/退回的份额账。

    所有变动以 append-only 记录保存；冲销不删除旧记录，只追加状态与双向链接。
    金额内部以分（int）存储，对外同时给出元值。
    """

    def __init__(self, currency="CNY"):
        self.currency = currency
        self.customers = {}
        self.issuers = {}
        self.invoices = OrderedDict()
        self.bank_flows = OrderedDict()
        self.instructions = OrderedDict()
        self.allocations = []
        self.advances = OrderedDict()
        self.credit_notes = []
        self.reversals = []
        self.events = []
        self._seq = 0

    # ------------------------------------------------------------ 基础登记
    def _next(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def _emit(self, event_type, payload):
        event = {"seq": len(self.events) + 1, "type": event_type, **payload}
        self.events.append(event)
        return event

    def add_customer(self, customer_id, name=None, authorized_signers=None,
                     payer_accounts=None, aliases=None, enforced=True, **extra):
        signers = set(authorized_signers or [])
        accounts = set(payer_accounts or [])
        alias_set = set(aliases or [])
        record = {
            "id": customer_id,
            "name": name or customer_id,
            "aliases": alias_set,
            "authorized_signers": signers,
            "payer_accounts": accounts,
            "enforced": enforced,
        }
        record.update(extra)
        self.customers[customer_id] = record
        # 别名也能解析回同一客户；若别名此前只是 ensure_customer 建的裸档案，
        # 合并后重新指向正式档案，避免同一主体出现两个头寸。
        for alias in alias_set:
            if not alias:
                continue
            existing = self.customers.get(alias)
            if isinstance(existing, dict) and existing is not record:
                if existing.get("enforced", True):
                    continue  # 两个正式主体抢同一别名，保留先登记者
                record["aliases"] |= existing.get("aliases", set())
                record["authorized_signers"] |= existing.get("authorized_signers", set())
                record["payer_accounts"] |= existing.get("payer_accounts", set())
            self.customers[alias] = record
        return record

    def ensure_customer(self, customer_id):
        """登记一个在发票/流水中出现、但没有正式主体档案的客户（仅做一致性比对）。"""
        if customer_id is None:
            return None
        existing = self._resolve_customer(customer_id)
        if existing is not None:
            return existing
        return self.add_customer(customer_id, enforced=False)

    def _same_customer(self, a, b):
        if a is None or b is None:
            return True  # 两边都没有主体信息时不臆判
        if a == b:
            return True
        ra, rb = self._resolve_customer(a), self._resolve_customer(b)
        if ra is not None and rb is not None:
            return ra["id"] == rb["id"]
        return False

    def add_issuer(self, issuer_id, legal_name=None, receivable_accounts=None, **extra):
        record = {
            "id": issuer_id,
            "legal_name": legal_name or issuer_id,
            "receivable_accounts": set(receivable_accounts or []),
        }
        record.update(extra)
        self.issuers[issuer_id] = record
        return record

    def add_invoice(self, invoice_id, amount, customer=None, issuer=None,
                    issuer_account=None, currency=None, void=False, status=None,
                    **extra):
        record = {
            "id": invoice_id,
            "amount_cents": to_cents(amount),
            "amount": from_cents(to_cents(amount)),
            "customer": customer,
            "issuer": issuer,
            "issuer_account": issuer_account,
            "currency": currency or self.currency,
            "void": bool(void) or status in ("void", "voided", "cancelled"),
            "status_hint": status,
        }
        record.update(extra)
        self.invoices[invoice_id] = record
        self._emit("invoice_registered", {"invoice_id": invoice_id,
                                          "amount": record["amount"]})
        return record

    def add_bank_entry(self, bank_id=None, amount=None, *, id=None, **fields):
        """登记一笔真实银行流水（入账 credit；也可直接登记 return/debit）。"""
        bank_id = bank_id or id or fields.get("bank_id") or self._next("bank")
        if amount is None and "amount_cents" not in fields:
            raise AllocationError("银行流水必须有 amount")
        cents = fields.pop("amount_cents", None)
        cents = cents if cents is not None else to_cents(amount)
        kind = fields.pop("kind", "credit")
        record = {
            "id": bank_id,
            "kind": kind,
            "amount_cents": cents,
            "amount": from_cents(cents),
            "currency": fields.pop("currency", self.currency),
            "payer": fields.pop("payer", None),
            "payer_account": fields.pop("payer_account", None),
            "payee_account": fields.pop("payee_account", None)
                             or fields.pop("account", None),
            "invoice_refs": list(fields.pop("invoice_refs", []) or []),
            "narration": fields.pop("narration", None),
            "at": fields.pop("at", None) or fields.pop("booked_at", None),
            "status": "booked",
            "reversed_by": None,
            "reverses": fields.pop("reverses", None),
        }
        record.update(fields)
        self.bank_flows[bank_id] = record
        self._emit("bank_entry_recorded",
                   {"bank_id": bank_id, "kind": kind, "amount": record["amount"]})
        return record

    # 旧名字别名
    def capture_remittance(self, *a, **kw):
        return self.add_bank_entry(*a, **kw)

    # ------------------------------------------------------------ 贷项通知
    def add_credit_note(self, invoice_id, amount, credit_note_id=None, *,
                        id=None, reason=None, at=None, **extra):
        if invoice_id not in self.invoices:
            raise AllocationError(f"贷项通知指向不存在的发票 {invoice_id}")
        cn_id = credit_note_id or id or self._next("CN")
        cents = to_cents(amount)
        if cents <= 0:
            raise AllocationError("贷项金额必须为正")
        inv = self.invoices[invoice_id]
        already = sum(c["amount_cents"] for c in self.credit_notes
                      if c["invoice_id"] == invoice_id and c["status"] == "active")
        if already + cents > inv["amount_cents"]:
            raise AllocationError(
                f"发票 {invoice_id} 贷项合计 {from_cents(already + cents)} "
                f"超过票面 {inv['amount']}")
        note = {
            "id": cn_id,
            "invoice_id": invoice_id,
            "amount_cents": cents,
            "amount": from_cents(cents),
            "reason": reason or "贷项冲抵",
            "at": at,
            "status": "active",
        }
        note.update(extra)
        self.credit_notes.append(note)
        self._emit("credit_note_issued",
                   {"credit_note_id": cn_id, "invoice_id": invoice_id,
                    "amount": note["amount"]})
        return note

    issue_credit_note = add_credit_note

    # ------------------------------------------------------------ 查询辅助
    def _resolve_customer(self, customer_ref):
        if customer_ref is None:
            return None
        return self.customers.get(customer_ref)

    def _bank(self, bank_ref):
        bank = self.bank_flows.get(bank_ref)
        if bank is None:
            raise AllocationError(f"付款指令指向不存在的银行流水 {bank_ref}")
        return bank

    def _active_allocations(self, invoice_id=None):
        return [a for a in self.allocations
                if a["status"] == "active"
                and (invoice_id is None or a["invoice_id"] == invoice_id)]

    def credit_total_cents(self, invoice_id):
        return sum(c["amount_cents"] for c in self.credit_notes
                   if c["invoice_id"] == invoice_id and c["status"] == "active")

    def received_cents(self, invoice_id):
        """该票当前有效核销实收（含由预收结转而来的核销）。"""
        return sum(a["amount_cents"] for a in self._active_allocations(invoice_id))

    def outstanding_cents(self, invoice_id):
        """票面在贷项与有效核销之后剩余的可吸收额度（不考虑作废状态）。"""
        inv = self.invoices[invoice_id]
        return inv["amount_cents"] - self.credit_total_cents(invoice_id) \
            - self.received_cents(invoice_id)

    def invoice_is_closed(self, invoice_id):
        """作废或已被贷项全额冲抵 -> 不能再吸收新汇款。"""
        inv = self.invoices[invoice_id]
        if inv.get("void"):
            return True, "发票已作废"
        if inv.get("status_hint") in ("credited", "credit_closed", "void"):
            return True, "发票已被贷项冲抵结清"
        if inv["amount_cents"] - self.credit_total_cents(invoice_id) <= 0:
            return True, "发票已被贷项全额冲抵"
        return False, None

    def returned_total_cents(self, bank_id):
        return sum(f["amount_cents"] for f in self.bank_flows.values()
                   if f.get("kind") in ("return", "debit_reversal", "chargeback")
                   and f.get("reverses") == bank_id)

    def bank_active_direct_cents(self, bank_id):
        """该流水当前仍占用的“直接核销”份额（不含经预收结转的核销）。"""
        return sum(a["amount_cents"] for a in self.allocations
                   if a["bank_id"] == bank_id and a["status"] == "active"
                   and not a.get("advance_id"))

    def bank_active_advance_principal_cents(self, bank_id):
        """该流水当前仍有效的预收本金（无论是否已部分结转到票）。"""
        return sum(ad["amount_cents"] for ad in self.advances.values()
                   if ad["bank_id"] == bank_id and ad["status"] == "active")

    def bank_pending_cents(self, bank_id):
        """待核未分配份额 = 实收净额 - 已占用直接核销 - 有效预收本金 - 已退回。"""
        bank = self._bank(bank_id)
        if bank["kind"] != "credit":
            return 0
        return (bank["amount_cents"]
                - self.returned_total_cents(bank_id)
                - self.bank_active_direct_cents(bank_id)
                - self.bank_active_advance_principal_cents(bank_id))

    def advance_remaining_cents(self, advance_id):
        adv = self.advances[advance_id]
        converted = sum(a["amount_cents"] for a in self.allocations
                        if a.get("advance_id") == advance_id
                        and a["status"] == "active")
        return adv["amount_cents"] - converted

    # ------------------------------------------------------------ 指令校验
    def _instruction_parts(self, instruction):
        """兼容多种常见键名，抽出标准要素。"""
        bank_ref = instruction.get("bank_ref") or instruction.get("bank_id") \
            or instruction.get("remittance_id") or instruction.get("bank")
        customer = instruction.get("customer") or instruction.get("customer_id") \
            or instruction.get("payer")
        signed_by = instruction.get("signed_by") or instruction.get("signer") \
            or instruction.get("authorized_by")
        signers = instruction.get("signers")
        if signed_by is None and signers:
            signed_by = signers[0] if isinstance(signers, (list, tuple)) else signers
        payer_account = instruction.get("payer_account") \
            or instruction.get("pay_account") or instruction.get("from_account")
        raw_allocations = instruction.get("allocations")
        if raw_allocations is None:
            raw_allocations = instruction.get("applications") \
                or instruction.get("splits") \
                or instruction.get("invoice_allocations") \
                or instruction.get("distribution")
        raw_advances = instruction.get("advances")
        if raw_advances is None:
            raw_advances = instruction.get("prepayments") \
                or instruction.get("advance_allocations") \
                or instruction.get("advances_to_projects") or []
        return bank_ref, customer, signed_by, payer_account, raw_allocations, raw_advances

    def validate_instruction(self, instruction):
        """返回 (plan, errors)。plan 为标准化后的分配/预收分列表。"""
        errors = []
        inst_id = instruction.get("id") or instruction.get("instruction_id")

        bank_ref, customer, signed_by, payer_account, raw_allocs, raw_advs = \
            self._instruction_parts(instruction)

        if not bank_ref:
            errors.append("付款指令缺少 bank_ref（必须指明授权分配哪笔银行入账）")
            bank = None
        elif bank_ref not in self.bank_flows:
            errors.append(f"付款指令 {inst_id} 指向不存在的银行流水 {bank_ref}")
            bank = None
        else:
            bank = self.bank_flows[bank_ref]
            if bank["kind"] != "credit":
                errors.append(f"{bank_ref} 不是入账流水，不能据以分配")
            if self.returned_total_cents(bank_ref) >= bank["amount_cents"]:
                errors.append(f"{bank_ref} 已被银行退回/拒付，不能再分配")

        # ---- 签署主体 / 付款账户 / 客户一致性
        if bank is not None:
            if customer is None:
                customer = bank.get("payer")
            if bank.get("payer") and customer \
                    and not self._same_customer(customer, bank["payer"]):
                errors.append(
                    f"指令客户 {customer} 与银行流水付款主体 {bank['payer']} 不一致")

            if payer_account is None:
                payer_account = bank.get("payer_account")
            if bank.get("payer_account") and payer_account \
                    and payer_account != bank["payer_account"]:
                errors.append(
                    f"指令付款账户 {payer_account} 与实际来款账户 "
                    f"{bank['payer_account']} 不一致")

        crec = self._resolve_customer(customer) if customer is not None else None
        if crec is None and customer is not None and self.customers:
            errors.append(f"未登记的客户主体 {customer!r}")
        # 只有正式客户档案才强制签署名单/账户名单；最小化数据至少要求有签署人
        require_sig = getattr(self, "require_signature", True)
        if crec is not None and crec.get("enforced"):
            if signed_by is None and require_sig:
                errors.append(
                    f"客户 {customer} 的核销指令缺少签署主体 signed_by，"
                    "只有附言不构成分配授权")
            elif signed_by is not None and crec["authorized_signers"] \
                    and signed_by not in crec["authorized_signers"]:
                errors.append(
                    f"签署主体 {signed_by!r} 不在客户 {crec['id']} 的授权签署名单内")
            if payer_account and crec["payer_accounts"] \
                    and payer_account not in crec["payer_accounts"]:
                errors.append(
                    f"付款账户 {payer_account} 不属于客户 {crec['id']} 登记账户")
        elif signed_by is None and require_sig:
            errors.append("核销指令缺少签署主体 signed_by")

        # ---- 标准化逐票分配
        plan_allocs = []
        for idx, item in enumerate(raw_allocs):
            iid = item.get("invoice") or item.get("invoice_id") \
                or item.get("invoice_ref") or item.get("ref") or item.get("id")
            try:
                cents = to_cents(item.get("amount"))
            except AllocationError as exc:
                errors.append(str(exc))
                continue
            if cents <= 0:
                errors.append(f"对 {iid} 的分配金额必须为正")
                continue
            if iid not in self.invoices:
                errors.append(f"分配指向不存在的发票 {iid}")
                continue
            inv = self.invoices[iid]
            closed, reason = self.invoice_is_closed(iid)
            if closed:
                errors.append(f"发票 {iid} {reason}，不能吸收新汇款")
            else:
                # 同一张票在本指令内可能出现多行，合并试算
                prior_in_plan = sum(p["amount_cents"] for p in plan_allocs
                                    if p["invoice_id"] == iid)
                outstanding = self.outstanding_cents(iid) - prior_in_plan
                if cents > outstanding:
                    errors.append(
                        f"发票 {iid} 本次分配 {from_cents(cents)} 超过可收余额 "
                        f"{from_cents(max(outstanding, 0))}（禁止超额核销）")
            # ---- 票、客户、开票主体/收款账户对应
            if inv is not None:
                if inv.get("customer") and customer \
                        and not self._same_customer(inv["customer"], customer):
                    errors.append(
                        f"发票 {iid} 抬头客户 {inv['customer']} 与指令客户 "
                        f"{customer} 不对应")
                if bank is not None:
                    payee = bank.get("payee_account")
                    if inv.get("issuer_account") and payee \
                            and inv["issuer_account"] != payee:
                        errors.append(
                            f"来款入账账户 {payee} 与发票 {iid} 开票主体收款账户 "
                            f"{inv['issuer_account']} 不对应")
                    issuer = self.issuers.get(inv.get("issuer"))
                    if issuer is not None and payee \
                            and issuer["receivable_accounts"] \
                            and payee not in issuer["receivable_accounts"]:
                        errors.append(
                            f"来款账户 {payee} 不属于发票 {iid} 的开票主体 "
                            f"{issuer['id']} 登记账户")
            plan_allocs.append({
                "invoice_id": iid, "amount_cents": cents,
                "amount": from_cents(cents),
                "line_ref": item.get("ref") or item.get("id"),
            })

        # ---- 标准化预收
        plan_advs = []
        for item in raw_advs:
            try:
                cents = to_cents(item.get("amount"))
            except AllocationError as exc:
                errors.append(str(exc))
                continue
            if cents <= 0:
                errors.append("预收金额必须为正")
                continue
            plan_advs.append({
                "ref": item.get("ref") or item.get("id"),
                "project": item.get("project"),
                "amount_cents": cents,
                "amount": from_cents(cents),
            })

        # ---- 银行现金守恒：本指令全部新份额不得超过该流水剩余可用净额
        if bank is not None:
            new_commitment = sum(p["amount_cents"] for p in plan_allocs) \
                + sum(p["amount_cents"] for p in plan_advs)
            available = (bank["amount_cents"]
                         - self.returned_total_cents(bank_ref)
                         - self.bank_active_direct_cents(bank_ref)
                         - self.bank_active_advance_principal_cents(bank_ref))
            if new_commitment > available:
                errors.append(
                    f"指令 {inst_id or ''} 分配+预收合计 "
                    f"{from_cents(new_commitment)} 超过银行流水 {bank_ref} "
                    f"剩余可分配净额 {from_cents(max(available, 0))}；"
                    "同一笔现金不能多账重复使用")

        plan = {
            "instruction_id": inst_id,
            "bank_ref": bank_ref,
            "customer": (self._resolve_customer(customer) or {}).get("id", customer)
                        if customer is not None else None,
            "signed_by": signed_by,
            "payer_account": payer_account,
            "allocations": plan_allocs,
            "advances": plan_advs,
            "raw": instruction,
        }
        return plan, errors

    # ------------------------------------------------------------ 指令落地
    def apply_instruction(self, instruction):
        """校验并原子落地一条正式指令；任何错误都不改动账本。"""
        if not isinstance(instruction, dict):
            raise AllocationError("指令必须是 JSON 对象")
        inst_id = instruction.get("id") or instruction.get("instruction_id") \
            or self._next("INSTR")
        plan, errors = self.validate_instruction(instruction)
        if errors:
            raise AllocationError(errors)

        bank_ref = plan["bank_ref"]
        customer = plan["customer"]
        bank = self.bank_flows[bank_ref]
        allocation_ids = []
        advance_ids = []

        for line in plan["allocations"]:
            al_id = self._next("AL")
            record = {
                "id": al_id,
                "invoice_id": line["invoice_id"],
                "amount_cents": line["amount_cents"],
                "amount": line["amount"],
                "bank_id": bank_ref,
                "instruction_id": inst_id,
                "advance_id": None,
                "customer": customer,
                "at": instruction.get("at"),
                "status": "active",
                "reversed_by": None,
            }
            self.allocations.append(record)
            allocation_ids.append(al_id)
            self._emit("invoice_allocated",
                       {"allocation_id": al_id,
                        "invoice_id": line["invoice_id"],
                        "bank_id": bank_ref, "amount": line["amount"]})

        for line in plan["advances"]:
            adv_id = line["ref"] or self._next("ADV")
            if adv_id in self.advances:
                adv_id = self._next("ADV")
            adv = {
                "id": adv_id,
                "customer": customer or bank.get("payer"),
                "bank_id": bank_ref,
                "instruction_id": inst_id,
                "ref": line["ref"],
                "project": line["project"],
                "amount_cents": line["amount_cents"],
                "amount": line["amount"],
                "at": instruction.get("at"),
                "status": "active",
                "conversion_ids": [],
                "reversed_by": None,
            }
            self.advances[adv_id] = adv
            advance_ids.append(adv_id)
            self._emit("advance_recognized",
                       {"advance_id": adv_id, "bank_id": bank_ref,
                        "customer": adv["customer"], "amount": line["amount"]})

        stored = {
            "id": inst_id,
            "bank_ref": bank_ref,
            "customer": customer,
            "signed_by": plan["signed_by"],
            "signature": instruction.get("signature"),
            "payer_account": plan["payer_account"],
            "at": instruction.get("at"),
            "allocations": [{"invoice": a["invoice_id"], "amount": a["amount"]}
                            for a in plan["allocations"]],
            "advances": [
                {"ref": line["ref"] or adv_id,
                 "advance_id": adv_id,
                 "project": line["project"], "amount": line["amount"]}
                for line, adv_id in zip(plan["advances"], advance_ids)
            ],
            "status": "applied",
            "errors": [],
            "allocation_ids": allocation_ids,
            "advance_ids": advance_ids,
        }
        self.instructions[inst_id] = stored
        self._emit("instruction_applied",
                   {"instruction_id": inst_id, "bank_id": bank_ref,
                    "allocation_ids": allocation_ids,
                    "advance_ids": advance_ids})
        return {"ok": True, "instruction_id": inst_id,
                "allocation_ids": allocation_ids, "advance_ids": advance_ids}

    def submit_instruction(self, instruction):
        """接收一条指令：有效则落地，无效则留存为 rejected 并返回错误（不抛出）。"""
        inst_id = (instruction or {}).get("id") if isinstance(instruction, dict) else None
        try:
            return self.apply_instruction(instruction)
        except AllocationError as exc:
            rid = inst_id or self._next("INSTR-REJ")
            # 绝不用 rejected 记录覆盖一条已 applied 的同名指令（保留原指令痕迹）
            existing = self.instructions.get(rid)
            if existing is not None and existing.get("status") == "applied":
                rid = self._next(f"{inst_id or 'INSTR'}-rejected")
            self.instructions[rid] = {
                "id": rid,
                "requested_id": inst_id if rid != inst_id else None,
                "status": "rejected",
                "errors": exc.errors,
                "raw": instruction,
                "allocation_ids": [],
                "advance_ids": [],
            }
            self._emit("instruction_rejected",
                       {"instruction_id": rid, "errors": exc.errors})
            return {"ok": False, "instruction_id": rid, "errors": exc.errors}

    # ------------------------------------------------------------ 预收转票
    def convert_advance(self, advance_ref, invoice_id, amount=None, *,
                        allocation_id=None, at=None, note=None):
        """预收日后转票核销；建立 advance <-> allocation 双向链。"""
        adv = self.advances.get(advance_ref)
        if adv is None:
            # 允许用 ref 业务编号找
            adv = next((a for a in self.advances.values()
                        if a.get("ref") == advance_ref), None)
        if adv is None:
            raise AllocationError(f"预收 {advance_ref} 不存在")
        if adv["status"] != "active":
            raise AllocationError(f"预收 {adv['id']} 已 {adv['status']}，不能结转")
        if invoice_id not in self.invoices:
            raise AllocationError(f"预收结转指向不存在的发票 {invoice_id}")
        closed, reason = self.invoice_is_closed(invoice_id)
        if closed:
            raise AllocationError(f"发票 {invoice_id} {reason}，不能预收结转")

        remaining = self.advance_remaining_cents(adv["id"])
        cents = remaining if amount is None else to_cents(amount)
        if cents <= 0:
            raise AllocationError("预收结转金额必须为正")
        if cents > remaining:
            raise AllocationError(
                f"预收 {adv['id']} 可结转余额仅 {from_cents(remaining)}，"
                f"不能结转 {from_cents(cents)}")
        outstanding = self.outstanding_cents(invoice_id)
        if cents > outstanding:
            raise AllocationError(
                f"发票 {invoice_id} 可收余额仅 {from_cents(outstanding)}，"
                f"预收结转 {from_cents(cents)} 超额")
        inv = self.invoices[invoice_id]
        if inv.get("customer") and adv.get("customer") \
                and not self._same_customer(inv["customer"], adv["customer"]):
            raise AllocationError(
                f"预收客户 {adv['customer']} 与发票 {invoice_id} 抬头 "
                f"{inv['customer']} 不一致")

        al_id = allocation_id or self._next("AL")
        record = {
            "id": al_id,
            "invoice_id": invoice_id,
            "amount_cents": cents,
            "amount": from_cents(cents),
            "bank_id": adv["bank_id"],
            "instruction_id": adv["instruction_id"],
            "advance_id": adv["id"],
            "customer": adv.get("customer"),
            "at": at,
            "note": note or "预收转票",
            "status": "active",
            "reversed_by": None,
        }
        self.allocations.append(record)
        adv["conversion_ids"].append(al_id)  # 反向链：预收 -> 核销
        self._emit("advance_converted",
                   {"advance_id": adv["id"], "allocation_id": al_id,
                    "invoice_id": invoice_id, "amount": record["amount"]})
        return record

    # ------------------------------------------------------------ 银行退回
    def reverse_bank_entry(self, bank_ref, *, reason="银行退回/拒付",
                           reversal_id=None, return_flow_id=None, at=None):
        """银行退回/拒付 bank_ref：逐份额反向冲销并保留原流水。

        - 原 bank_ref 流水原样保留，另开一笔 kind=return 的反向流水；
        - 该流水实际占用的每一条 allocation 都置为 reversed（含预收结转形成的
          核销），每一笔有效预收都冲回；待核余额随退款一并退出；
        - 冲销记录列出具体 allocation_id / advance_id，而不是只把总额减 1000。
        """
        bank = self._bank(bank_ref)
        if bank["kind"] != "credit":
            raise AllocationError(f"{bank_ref} 不是入账流水，不能做退回冲销")
        if self.returned_total_cents(bank_ref) >= bank["amount_cents"]:
            raise AllocationError(f"{bank_ref} 已全额退回，不能重复冲销")

        rev_id = reversal_id or self._next("REV")
        # 退回前先按“当前仍生效”的份额算出待核余额与每笔预收的未结转余额。
        # 必须在任何 status 翻转之前计算，否则已被标记 reversed 的结转核销会让
        # 预收余额反弹，造成冲销分类重复计数。
        unallocated = self.bank_pending_cents(bank_ref)
        advance_remaining = {
            adv["id"]: self.advance_remaining_cents(adv["id"])
            for adv in self.advances.values()
            if adv["bank_id"] == bank_ref and adv["status"] == "active"
        }

        # 该流水实际占用的每一条核销（含预收结转形成的核销）逐条反向冲销
        targets = [a for a in self.allocations
                   if a["bank_id"] == bank_ref and a["status"] == "active"]
        alloc_ids = []
        alloc_total = 0
        for al in targets:
            al["status"] = "reversed"
            al["reversed_by"] = rev_id
            alloc_ids.append(al["id"])
            alloc_total += al["amount_cents"]
            self._emit("allocation_reversed",
                       {"allocation_id": al["id"], "reversal_id": rev_id,
                        "invoice_id": al["invoice_id"]})

        adv_ids = []
        adv_remaining_total = 0
        for adv in self.advances.values():
            if adv["bank_id"] == bank_ref and adv["status"] == "active":
                rem = advance_remaining.get(adv["id"], 0)
                adv["status"] = "reversed"
                adv["reversed_by"] = rev_id
                adv_ids.append(adv["id"])
                adv_remaining_total += rem
                self._emit("advance_reversed",
                           {"advance_id": adv["id"], "reversal_id": rev_id,
                            "remaining_extinguished": from_cents(rem)})

        flow_id = return_flow_id or f"{bank_ref}-return"
        if flow_id in self.bank_flows:
            flow_id = self._next("bank-return")
        # 反向流水仍归属原付款客户（便于客户头寸归并），收款方向通过 kind/reverses 表达
        return_flow = self.add_bank_entry(
            flow_id, bank["amount"], kind="return", reverses=bank_ref,
            reason=reason, at=at, payer=bank.get("payer"),
            payer_account=bank.get("payer_account"),
            account=bank.get("payee_account"),
            returned_from_account=bank.get("payee_account"))

        bank["status"] = "reversed"
        bank["reversed_by"] = rev_id

        reversal = {
            "id": rev_id,
            "kind": "bank_return",
            "reason": reason,
            "bank_id": bank_ref,
            "return_flow_id": flow_id,
            "at": at,
            "allocation_ids": alloc_ids,
            "allocation_total": from_cents(alloc_total),
            "advance_ids": adv_ids,
            "advance_remaining_total": from_cents(adv_remaining_total),
            "unallocated_total": from_cents(unallocated),
            "total": from_cents(alloc_total + adv_remaining_total + unallocated),
        }
        self.reversals.append(reversal)
        self._emit("bank_entry_reversed",
                   {"reversal_id": rev_id, "bank_id": bank_ref,
                    "allocation_ids": alloc_ids, "advance_ids": adv_ids})
        return reversal

    # 常用别名
    return_remittance = reverse_bank_entry
    chargeback = reverse_bank_entry
    refund_bank_entry = reverse_bank_entry


# 语义化别名
RemittanceCapture = RemittanceBook


# ---------------------------------------------------------------- 模块级函数（函数式入口）

def capture_bank_entry(book, *args, **kwargs):
    return book.add_bank_entry(*args, **kwargs)


def validate_instruction(book, instruction):
    return book.validate_instruction(instruction)


def allocate(book, instruction):
    return book.apply_instruction(instruction)


def reverse_bank_entry(book, bank_ref, **kwargs):
    return book.reverse_bank_entry(bank_ref, **kwargs)
