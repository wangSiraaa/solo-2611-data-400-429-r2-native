"""WAL-driven transaction recovery for in-place delta pages.

Recovery progress is stored in the WAL itself (no separate progress file), so
every pass is safe to repeat after a crash at any point.

Analysis
    Read the durable prefix (the real wal.jsonl reopened from disk) and
    classify each transaction: COMMIT -> winner, ABORT -> already rolled back,
    anything still active -> loser this recovery must abort.

Undo pass
    Every loser ADD without a durable UNDO gets a physical compensation record
    under the existing delta/UNDO/undo_of/undo_next contract, each forced (real
    write + fsync) BEFORE any page IO.  A fresh ABORT decision is then forced
    per loser.  Dying after the force but before the page write is harmless: the
    next run finds the UNDO durable and never appends a duplicate (undo_of
    de-duplicates compensations).

Redo plan + execution
    page_lsn alone cannot decide membership after an interrupted recovery (a
    recovery-written page can regress relative to another image, and a crash
    page can hold an ADD whose UNDO was never flushed).  Before any recovery
    page write we therefore append and FORCE one RECOVER_BEGIN record per
    affected page.  It freezes the crash-time base_lsn and the exact ordered
    list of physical LSNs to apply.  Repeated recoveries reuse that durable plan
    instead of re-guessing membership.  Applying a planned LSN raises page_lsn
    to it, so an lsn-vs-page_lsn guard makes every physical change apply at most
    once even when an earlier run already wrote part of the result.

A page whose page_lsn is beyond the real durable WAL end cannot be repaired
(the inverse delta was never persisted); recovery fails closed instead of
guessing a value.
"""
import os
from pathlib import Path

from page_io import Pages
from wal import Journal

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

PHYSICAL_KINDS = ('ADD', 'UNDO')
TERMINAL_KINDS = ('COMMIT', 'ABORT')

# Exit codes identify which real persistence boundary an injected stop hit.
EXIT_UNDO_PAGE = 75      # a recovery page write has been persisted
EXIT_UNDO_LOG = 76       # an UNDO was forced but its page write did not happen
EXIT_ABORT_LOG = 77      # an ABORT decision was forced, redo did not run
EXIT_BEFORE_READY = 78   # all work persisted, READY publication interrupted


class RecoveryError(RuntimeError):
    """Directory is not recoverable; the caller must fail closed."""


def _analyze(records):
    transactions = {}
    for record in records:
        # RECOVER_BEGIN is recovery bookkeeping, not a business transaction.
        if record['kind'] == 'RECOVER_BEGIN':
            continue
        state = transactions.setdefault(
            record['tx'], {'last_lsn': 0, 'state': 'active',
                           'adds': [], 'undos': []})
        state['last_lsn'] = record['lsn']
        if record['kind'] in TERMINAL_KINDS:
            state['state'] = record['kind']
        elif record['kind'] == 'ADD':
            state['adds'].append(record)
        elif record['kind'] == 'UNDO':
            state['undos'].append(record)
    return transactions


def _validate_wal(records):
    expected = 1
    for record in records:
        if record.get('lsn') != expected:
            raise RecoveryError(
                'wal.jsonl is not a complete LSN prefix: expected lsn %s, got %r'
                % (expected, record.get('lsn')))
        for field in ('kind', 'tx', 'prev_lsn'):
            if field not in record:
                raise RecoveryError('wal record lsn %s missing %s' % (expected, field))
        if record['kind'] in PHYSICAL_KINDS and (
                'page' not in record or 'key' not in record or 'delta' not in record):
            raise RecoveryError('physical record lsn %s missing page/key/delta' % expected)
        expected += 1


def _validate_pages(pages, records, durable_lsn):
    physical = {record['lsn']: record for record in records
                if record['kind'] in PHYSICAL_KINDS}
    result = {}
    for page_id in pages.list_pages():
        page = pages.read(page_id)
        if page.get('page_id') != page_id:
            raise RecoveryError('page file %s.json has mismatched page_id' % page_id)
        page_lsn = page.get('page_lsn')
        if not isinstance(page_lsn, int) or isinstance(page_lsn, bool) or page_lsn < 0:
            raise RecoveryError('page %s has invalid page_lsn %r' % (page_id, page_lsn))
        rows = page.get('rows')
        if not isinstance(rows, dict) or not rows:
            raise RecoveryError('page %s has invalid rows' % page_id)
        for key, value in rows.items():
            if not isinstance(key, str) or not isinstance(value, int) or isinstance(value, bool):
                raise RecoveryError('page %s row %s has a non-int64 value' % (page_id, key))
            if not INT64_MIN <= value <= INT64_MAX:
                raise RecoveryError('page %s row %s value is outside int64' % (page_id, key))
        # Hard durability invariant: every physical change the page claims must
        # exist in the real, reopenable WAL prefix.
        if page_lsn > durable_lsn:
            raise RecoveryError(
                'page %s page_lsn=%s is beyond the durable WAL end lsn=%s; '
                'the directory is an unsafe-flush state and cannot be repaired'
                % (page_id, page_lsn, durable_lsn))
        if page_lsn and page_lsn not in physical:
            raise RecoveryError(
                'page %s page_lsn=%s is not a physical ADD/UNDO record' % (page_id, page_lsn))
        if page_lsn and physical[page_lsn]['page'] != page_id:
            raise RecoveryError(
                'page %s page_lsn=%s belongs to a different page' % (page_id, page_lsn))
        result[page_id] = page
    return result


def inspect(root):
    """Read-only view of what recovery would see; never writes or forces."""
    root = Path(root)
    journal = Journal(root)
    pages = Pages(root / 'pages')
    records = journal.durable()
    _validate_wal(records)
    durable_lsn = records[-1]['lsn'] if records else 0
    on_disk = _validate_pages(pages, records, durable_lsn)
    transactions = _analyze(records)
    return {
        'status': 'inspected',
        'durable_lsn': durable_lsn,
        'wal_records': records,
        'pages': {page_id: pages.read(page_id) for page_id in sorted(on_disk)},
        'transactions': {tx: state['state'] for tx, state in transactions.items()},
        'compensated_add_lsns': sorted(record['undo_of'] for record in records
                                       if record['kind'] == 'UNDO'),
        'redo_plans': {record['page']: {'base_lsn': record['base_lsn'],
                                        'apply_lsns': record['apply_lsns']}
                       for record in records if record['kind'] == 'RECOVER_BEGIN'},
        'unsafe_pages': sorted(page_id for page_id, page in on_disk.items()
                               if page['page_lsn'] > durable_lsn),
        'journal_audit': journal.audit,
    }


def recover(root, crash_after=None, fault=None):
    root = Path(root)
    journal = Journal(root)
    pages = Pages(root / 'pages')

    records = journal.durable()
    _validate_wal(records)
    durable_lsn = records[-1]['lsn'] if records else 0
    on_disk = _validate_pages(pages, records, durable_lsn)

    # ---- Analysis ---------------------------------------------------------
    transactions = _analyze(records)
    winners = {tx for tx, state in transactions.items() if state['state'] == 'COMMIT'}
    already_aborted = {tx for tx, state in transactions.items()
                       if state['state'] == 'ABORT'}
    losers = sorted(tx for tx, state in transactions.items()
                    if state['state'] == 'active')
    compensated = {record['undo_of'] for record in records
                   if record['kind'] == 'UNDO'}

    # ---- Undo pass: durable compensations before any page IO --------------
    appended_undos = []
    appended_aborts = []
    for tx in losers:
        state = transactions[tx]
        for add_record in reversed(state['adds']):
            if add_record['lsn'] in compensated:
                continue
            undo_lsn = journal.append({
                'kind': 'UNDO', 'tx': tx, 'prev_lsn': state['last_lsn'],
                'page': add_record['page'], 'key': add_record['key'],
                'delta': -add_record['delta'],
                'undo_of': add_record['lsn'],
                'undo_next': add_record['prev_lsn']})
            journal.force(undo_lsn)
            state['last_lsn'] = undo_lsn
            compensated.add(add_record['lsn'])
            appended_undos.append(undo_lsn)
            if fault == 'after-undo-log' or (fault is None and crash_after == 0):
                os._exit(EXIT_UNDO_LOG)
        abort_lsn = journal.append({'kind': 'ABORT', 'tx': tx,
                                    'prev_lsn': state['last_lsn']})
        journal.force(abort_lsn)
        state['state'] = 'ABORT'
        state['last_lsn'] = abort_lsn
        appended_aborts.append(abort_lsn)
        if fault == 'after-abort':
            os._exit(EXIT_ABORT_LOG)

    # Re-open the real file so everything below reads one durable prefix that
    # already includes any UNDO/ABORT forced by an earlier interrupted run.
    records = journal.durable()
    _validate_wal(records)
    winner_txns = set(winners)
    loser_txns = set(losers)
    compensated = {record['undo_of'] for record in records
                   if record['kind'] == 'UNDO'}
    add_index = {record['lsn']: record for record in records
                 if record['kind'] == 'ADD'}

    # ---- Redo planning ----------------------------------------------------
    # Freeze one durable plan per affected page BEFORE writing any page.
    # Membership relative to the crash-time base_lsn:
    #   winner ADD above base            -> apply (missing committed increment)
    #   compensated loser ADD            -> never re-applied as a business ADD
    #   UNDO whose ADD is at/below base   -> apply (erase loser delta on the page)
    #   UNDO whose ADD is above base      -> skip (ADD/UNDO pair never on page)
    def build_plan(page_id, base_lsn):
        plan = []
        for record in records:
            if record['kind'] not in PHYSICAL_KINDS or record['page'] != page_id:
                continue
            if record['lsn'] <= base_lsn:
                continue
            if record['kind'] == 'ADD':
                if record['lsn'] in compensated:
                    continue
                if record['tx'] not in winner_txns:
                    raise RecoveryError(
                        'non-winner ADD lsn=%s has no durable UNDO; refusing to replay'
                        % record['lsn'])
                plan.append(record['lsn'])
            else:
                add_record = add_index.get(record['undo_of'])
                if add_record is None or add_record['tx'] in winner_txns:
                    raise RecoveryError(
                        'UNDO lsn=%s references unknown or committed ADD lsn=%s'
                        % (record['lsn'], record['undo_of']))
                if add_record['lsn'] <= base_lsn:
                    plan.append(record['lsn'])
        return plan

    touched_pages = sorted({record['page'] for record in records
                            if record['kind'] in PHYSICAL_KINDS})
    # Keep the EARLIEST durable plan per page.  A plan stays authoritative while
    # no new physical ADD/UNDO was appended after it (i.e. recovery was still in
    # progress when the process died).  Once later business transactions exist,
    # every old plan is stale and a fresh one is built from the current pages.
    existing_plans = {}
    for record in records:
        if record['kind'] == 'RECOVER_BEGIN' and record['page'] not in existing_plans:
            existing_plans[record['page']] = record
    last_physical_lsn = max((record['lsn'] for record in records
                             if record['kind'] in PHYSICAL_KINDS), default=0)
    plans = {}
    appended_plans = []
    for page_id in touched_pages:
        prior = existing_plans.get(page_id)
        if prior is not None and prior['lsn'] > last_physical_lsn:
            # Plan was written after the last business/undo physical record:
            # this recovery (or an interrupted earlier one) owns the page.
            plans[page_id] = {'base_lsn': prior['base_lsn'],
                              'apply_lsns': list(prior['apply_lsns'])}
            continue
        base_lsn = pages.read(page_id)['page_lsn']
        apply_lsns = build_plan(page_id, base_lsn)
        plan_lsn = journal.append({
            'kind': 'RECOVER_BEGIN', 'tx': '', 'prev_lsn': 0,
            'page': page_id, 'base_lsn': base_lsn, 'apply_lsns': apply_lsns})
        journal.force(plan_lsn)
        appended_plans.append(plan_lsn)
        plans[page_id] = {'base_lsn': base_lsn, 'apply_lsns': apply_lsns}

    # Re-open after forcing the plans; all page writes below are then covered by
    # this durable prefix (WAL-before-page inside recovery).
    records = journal.durable()
    _validate_wal(records)
    final_durable_lsn = records[-1]['lsn'] if records else 0
    records_by_lsn = {record['lsn']: record for record in records}

    # ---- Redo execution: converge each page to its frozen plan ------------
    redo_report = {}
    page_writes = 0
    for page_id in touched_pages:
        current = pages.read(page_id)
        from_lsn = current['page_lsn']
        planned = plans[page_id]['apply_lsns']
        applied, skipped = [], []
        changed = False
        for lsn in planned:
            if lsn <= current['page_lsn']:
                # Already persisted by an earlier interrupted recovery run.
                skipped.append(lsn)
                continue
            record = records_by_lsn[lsn]
            if record['key'] not in current['rows']:
                raise RecoveryError('record lsn=%s touches unknown key %s on page %s'
                                    % (lsn, record['key'], page_id))
            new_value = current['rows'][record['key']] + record['delta']
            if not INT64_MIN <= new_value <= INT64_MAX:
                raise RecoveryError(
                    'redo of lsn=%s would put %s/%s outside int64'
                    % (lsn, page_id, record['key']))
            current['rows'][record['key']] = new_value
            current['page_lsn'] = lsn
            applied.append(lsn)
            changed = True
        if changed:
            if final_durable_lsn < current['page_lsn']:
                raise RecoveryError('attempted page write beyond durable WAL prefix')
            pages.write(page_id, current)
            page_writes += 1
            if (fault == 'after-redo-write' or
                    (fault is None and isinstance(crash_after, int) and crash_after > 0
                     and crash_after == page_writes)):
                os._exit(EXIT_UNDO_PAGE)
        redo_report[page_id] = {
            'from_lsn': from_lsn, 'to_lsn': current['page_lsn'],
            'base_lsn': plans[page_id]['base_lsn'],
            'planned_lsns': planned, 'applied_lsns': applied,
            'skipped_lsns': skipped, 'page_written': changed}

    if fault == 'before-ready':
        os._exit(EXIT_BEFORE_READY)

    return {
        'status': 'ready',
        'durable_lsn': final_durable_lsn,
        'winner_transactions': sorted(winner_txns),
        'aborted_transactions': sorted(loser_txns),
        'already_aborted_before_recovery': sorted(already_aborted),
        'appended_undo_lsns': appended_undos,
        'appended_abort_lsns': appended_aborts,
        'appended_plan_lsns': appended_plans,
        'redo': redo_report,
        'actual_page_writes': page_writes,
        'validated_pages': sorted(on_disk),
        'journal_audit': journal.audit,
        'page_audit': pages.audit,
    }
