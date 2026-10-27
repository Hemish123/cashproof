# from decimal import Decimal

# TOL = Decimal('0.05')

# def _d(v):
#     try:
#         return Decimal(str(v)) if v is not None else Decimal('0.00')
#     except Exception:
#         return Decimal('0.00')

# def _check(calc, reported):
#     if reported is None:
#         return None                      # printed balance not available -> not verified
#     return abs(calc - _d(reported)) <= TOL

# def reconcile_three_step(acc, txs):
#     """None for normal statements. For reconciliation reports returns the 3-step result."""
#     if not any(getattr(t, 'cleared_status', None) for t in txs):
#         return None

#     def bucket(status):
#         amts = [_d(t.amount) for t in txs if t.cleared_status == status]
#         dep = sum((a for a in amts if a > 0), Decimal('0.00'))
#         wd = abs(sum((a for a in amts if a < 0), Decimal('0.00')))
#         return dep, wd

#     beg = _d(acc.beginning_balance)
#     c_dep, c_wd = bucket('cleared')
#     u_dep, u_wd = bucket('uncleared')
#     n_dep, n_wd = bucket('new')

#     cleared_closing = beg + c_dep - c_wd                       # step 1
#     register = cleared_closing + u_dep - u_wd                  # step 2
#     ending = register + n_dep - n_wd                           # step 3

#     steps = [
#         {'label': '1. Beginning + Cleared Deposits - Cleared Withdrawals = Cleared Balance',
#          'calc': cleared_closing, 'reported': acc.cleared_balance,
#          'ok': _check(cleared_closing, acc.cleared_balance)},
#         {'label': '2. Cleared Balance + Uncleared Deposits - Uncleared Withdrawals = Register Balance',
#          'calc': register, 'reported': acc.register_balance,
#          'ok': _check(register, acc.register_balance)},
#         {'label': '3. Register Balance + New Deposits - New Withdrawals = Ending Balance',
#          'calc': ending, 'reported': acc.ending_balance,
#          'ok': _check(ending, acc.ending_balance)},
#     ]
#     unclassified = sum(1 for t in txs if t.cleared_status not in ('cleared', 'uncleared', 'new'))
#     passed = unclassified == 0 and all(s['ok'] is not False for s in steps) and steps[2]['ok'] is True

#     return {
#         'beginning': beg,
#         'cleared': (c_dep, c_wd), 'uncleared': (u_dep, u_wd), 'new': (n_dep, n_wd),
#         'steps': steps, 'unclassified': unclassified, 'passed': passed,
#     }



from decimal import Decimal

TOL = Decimal('0.05')
GROUPS = ('cleared', 'uncleared', 'new')


def _d(v):
    try:
        return Decimal(str(v)) if v is not None else Decimal('0.00')
    except Exception:
        return Decimal('0.00')


def _opt(v):
    """Decimal, or None when the value is missing."""
    if v is None or v == '':
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _check(calc, reported):
    if reported is None:
        return None                      # printed balance not available -> not verified
    return abs(calc - _d(reported)) <= TOL


def reconcile_three_step(acc, txs):
    """None for normal statements. For reconciliation reports returns the 3-step result.

    The group figures (deposits / payments) come from the PRINTED 'Total ...' lines of the
    report whenever they were extracted (acc.recon_totals). The extracted rows are only used
    to cross-check them (or as a fallback when a printed total is missing)."""
    if not any(getattr(t, 'cleared_status', None) for t in txs):
        return None

    printed = getattr(acc, 'recon_totals', None) or {}
    beg = _d(acc.beginning_balance)

    groups = {}
    for g in GROUPS:
        amts = [_d(t.amount) for t in txs if t.cleared_status == g]
        row_dep = sum((a for a in amts if a > 0), Decimal('0.00'))
        row_wd = abs(sum((a for a in amts if a < 0), Decimal('0.00')))
        row_net = row_dep - row_wd

        p = printed.get(g) or {}
        dep, wd, net = _opt(p.get('deposits')), _opt(p.get('payments')), _opt(p.get('net'))
        # A report omits a list that is empty (e.g. no uncleared deposits): derive it from the net.
        if dep is None and wd is not None and net is not None:
            dep = max(net + wd, Decimal('0.00'))
        if wd is None and dep is not None and net is not None:
            wd = max(dep - net, Decimal('0.00'))
        from_printed = dep is not None and wd is not None
        if dep is None:
            dep = row_dep
        if wd is None:
            wd = row_wd

        ref_net = net if net is not None else (dep - wd)
        groups[g] = {
            'deposits': dep, 'payments': wd, 'net': dep - wd,
            'source': 'printed' if from_printed else 'rows',
            'row_deposits': row_dep, 'row_payments': row_wd, 'row_net': row_net,
            'net_ok': abs(row_net - ref_net) <= TOL,                     # rows add up to the printed net
            'gross_ok': abs(row_dep - dep) <= TOL and abs(row_wd - wd) <= TOL,
        }

    c, u, n = groups['cleared'], groups['uncleared'], groups['new']
    cleared_closing = beg + c['deposits'] - c['payments']              # step 1
    register = cleared_closing + u['deposits'] - u['payments']         # step 2
    ending = register + n['deposits'] - n['payments']                  # step 3

    steps = [
        {'label': '1. Beginning + Cleared Deposits - Cleared Withdrawals = Cleared Balance',
         'calc': cleared_closing, 'reported': acc.cleared_balance,
         'ok': _check(cleared_closing, acc.cleared_balance)},
        {'label': '2. Cleared Balance + Uncleared Deposits - Uncleared Withdrawals = Register Balance',
         'calc': register, 'reported': acc.register_balance,
         'ok': _check(register, acc.register_balance)},
        {'label': '3. Register Balance + New Deposits - New Withdrawals = Ending Balance',
         'calc': ending, 'reported': acc.ending_balance,
         'ok': _check(ending, acc.ending_balance)},
    ]
    unclassified = sum(1 for t in txs if t.cleared_status not in GROUPS)
    # PASS/FAIL = the report's own arithmetic (printed totals through the 3 steps).
    passed = (unclassified == 0
              and all(s['ok'] is not False for s in steps) and steps[2]['ok'] is True)
    # Separate check: do the extracted transaction rows add up to the printed totals?
    extraction_ok = all(groups[g]['net_ok'] and groups[g]['gross_ok']
                        for g in GROUPS if groups[g]['source'] == 'printed')

    return {
        'beginning': beg,
        'cleared': (c['deposits'], c['payments']),
        'uncleared': (u['deposits'], u['payments']),
        'new': (n['deposits'], n['payments']),
        'groups': groups,
        'steps': steps, 'unclassified': unclassified, 'passed': passed,
        'extraction_ok': extraction_ok,
    }