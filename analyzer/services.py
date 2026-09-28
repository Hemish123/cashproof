from decimal import Decimal
import re
from datetime import timedelta
import numpy as np
from .models import BankAccount, Transaction

def _clean(s: str) -> str:
    return re.sub(r'[\s\-]', '', s or '')

def _business_day_window(date, max_business_days=2):
    lo = np.busday_offset(date, -max_business_days, roll='forward')
    hi = np.busday_offset(date, max_business_days, roll='backward')
    return lo.astype('datetime64[D]').tolist(), hi.astype('datetime64[D]').tolist()

def _date_confidence(outflow_date, inflow_date):
    diff = abs((inflow_date - outflow_date).days)
    if diff == 0:
        return 'very_high'
    if diff == 1:
        return 'high'
    return 'medium'

def detect_interbank_transactions(project_id=None):
    if project_id is not None:
        user_accounts = list(BankAccount.objects.filter(project_id=project_id))
        user_account_ids = [acc.id for acc in user_accounts]
        base_qs = Transaction.objects.filter(account_id__in=user_account_ids)
    else:
        user_accounts = list(BankAccount.objects.all())
        base_qs = Transaction.objects.all()

    base_qs.update(is_interbank=False, interbank_confidence=None, interbank_match_status=None)

    all_txs = list(base_qs.order_by('date', 'id'))

    acc_by_id = {acc.id: acc for acc in user_accounts}
    project_accounts = {}
    for acc in user_accounts:
        if acc.project_id not in project_accounts:
            project_accounts[acc.project_id] = []
        project_accounts[acc.project_id].append(acc)

    paired_txs = set()
    to_update = []

    for tx in all_txs:
        if tx.id in paired_txs:
            continue
            
        desc_cleaned = _clean(tx.description)
        own_acc = acc_by_id.get(tx.account_id)
        if not own_acc or not own_acc.project_id:
            continue

        target_amount = abs(tx.amount)
        min_date, max_date = _business_day_window(tx.date, max_business_days=3)
        
        sibling_accounts = [a for a in project_accounts[own_acc.project_id] if a.id != own_acc.id]
        
        matched_other = None
        confidence = None

        for sib in sibling_accounts:
            sib_num = _clean(sib.account_number)
            is_referenced = False
            
            if sib_num and sib_num.lower() != 'unknownaccount':
                if sib_num in desc_cleaned:
                    is_referenced = True
                elif len(sib_num) >= 4 and sib_num[-4:] in desc_cleaned:
                    is_referenced = True

            if is_referenced:
                candidates = []
                for other_tx in all_txs:
                    if other_tx.id in paired_txs or other_tx.account_id != sib.id:
                        continue
                    if abs(other_tx.amount) == target_amount and (other_tx.amount > 0) != (tx.amount > 0):
                        if min_date <= other_tx.date <= max_date:
                            candidates.append(other_tx)
                
                if candidates:
                    candidates.sort(key=lambda c: abs((c.date - tx.date).days))
                    matched_other = candidates[0]
                    confidence = _date_confidence(tx.date, matched_other.date)
                    break
                else:
                    # Referenced a sibling account but we don't have the matching transaction
                    tx.is_interbank = True
                    tx.interbank_confidence = 'low'
                    tx.interbank_match_status = 'unverified_sibling_reference'
                    if tx.id not in paired_txs:
                        to_update.append(tx)
                        # We don't add to paired_txs because we didn't find a pair, but it IS interbank
                    break
        
        if matched_other:
            tx.is_interbank = True
            tx.interbank_confidence = confidence
            tx.interbank_match_status = 'confirmed_account_and_amount'
            
            matched_other.is_interbank = True
            matched_other.interbank_confidence = confidence
            matched_other.interbank_match_status = 'confirmed_account_and_amount'
            
            paired_txs.add(tx.id)
            paired_txs.add(matched_other.id)
            to_update.extend([tx, matched_other])

    if to_update:
        unique_updates = {t.id: t for t in to_update}.values()
        Transaction.objects.bulk_update(
            unique_updates, ['is_interbank', 'interbank_confidence', 'interbank_match_status']
        )

    from .parsers.manager import update_account_aggregates
    for account in user_accounts:
        update_account_aggregates(account)

    return {
        'total_paired': len(paired_txs),
    }