"""Cross-check fixes: fractional spreadsheet qty, ratio-pack sizes, freight-aware values, xls cells, PI dates, id reuse."""
from datetime import date, datetime
from app import guard, db, extract
from app.schema import Order, PO, to_qty, parse_date, warnings_for


def test_float_noise_is_whole_and_real_fractions_round_with_warning():
    assert to_qty(559.9999999999999) == 560 and to_qty(1300.0000000000002) == 1300
    d = {'pos': [{'po_number': '2826966', 'lines': [{'style': 'S1', 'color': 'Navy', 'order_qty': 779.22, 'rate': 7.06, 'net_value': 5501.2932}]}]}
    notes = guard.soften(d)
    l = d['pos'][0]['lines'][0]
    assert l['order_qty'] == 779 and l['qty_rounded'] and l['_qty_raw'] == 779.22
    assert len(notes) == 1 and 'rounded' in notes[0]
    Order.model_validate(d)
    assert guard.arithmetic_errors(d) == []
    assert guard.text_errors(d, 'Navy\t2826966\t779.22\t7.06\t5501.2932') == []
    o = Order.model_validate(d)
    assert not [w for w in warnings_for(o) if 'rate x qty' in w]


def test_ratio_pack_sizes_are_accepted_but_invented_numbers_are_not():
    text = ("Ratio Pack Description   No of Singles per Ratio   Qty of Ratio Packs\n"
            "RATIO SET 2P BLU      4      130\n"
            "Colour Size Break up Qty\nBLUE 2/3 2\nBLUE 5/6 1\nTotal Quantity: 1040\n")
    ok = {'pos': [{'po_number': '460-68171', 'lines': [{'color': 'BLUE', 'sizes': {'2/3': 260, '5/6': 130}}]}]}
    assert guard.text_errors(ok, text) == [] or all('po_number' in e for e in guard.text_errors(ok, text))
    bad = {'pos': [{'po_number': '460-68171', 'lines': [{'color': 'BLUE', 'sizes': {'2/3': 777}}]}]}
    assert any('777' in e for e in guard.text_errors(bad, text))


def test_freight_line_value_and_total_are_consistent():
    d = {'pos': [{'po_number': 'PI1', 'stated_total_value': 5831.7, 'lines': [
        {'color': 'PINK', 'order_qty': 1820, 'rate': 2.25, 'net_value': 4070, 'freight': 25},
        {'color': 'OFF-WHITE', 'order_qty': 827, 'rate': 2.1, 'net_value': 1721.7, 'freight': 15}]}]}
    assert guard.arithmetic_errors(d) == []
    o = Order.model_validate(d)
    assert not [w for w in warnings_for(o) if 'rate x qty' in w or 'stated total value' in w]
    d['pos'][0]['lines'][0]['freight'] = None
    assert guard.arithmetic_errors(d)   # without the printed freight the mismatch is still reported


def test_repeated_guard_messages_are_shown_once():
    msgs = ['AI guard: X bad', 'AI guard: merged document: X bad', 'AI guard: [a] X bad', 'Other']
    assert guard.dedupe_messages(msgs) == ['AI guard: X bad', 'Other']


def test_xls_and_xlsx_cells_read_like_a_person():
    import xlrd
    assert extract.cell_text(46303.0, xlrd.XL_CELL_DATE, 0) == '2026-10-08'
    assert extract.cell_text(2600043657.0, xlrd.XL_CELL_NUMBER, 0) == '2600043657'
    assert extract.cell_text(779.2199999999999) == '779.22'
    assert extract.cell_text(datetime(2026, 11, 1)) == '2026-11-01'
    assert extract.cell_text('TBA-3') == 'TBA-3'


def test_pi_dates_and_yearless_ship_date():
    assert parse_date('September 21, 2026') == date(2026, 9, 21)
    assert parse_date('02-Nov-26') == date(2026, 11, 2)
    p = PO.model_validate({'po_number': 'PI1', 'po_date': 'September 21, 2026', 'ship_date': 'NOV-1 ETD'})
    assert p.ship_date == date(2026, 11, 1) and not p.date_raw
    p = PO.model_validate({'po_number': 'PI1', 'ship_date': 'NOV-1 ETD'})
    assert p.ship_date is None and p.date_raw['ship_date'] == 'NOV-1 ETD'


def test_deleted_order_id_is_never_reused():
    db.init()
    o = Order.model_validate({'buyer_name': 'B', 'pos': [{'po_number': 'P', 'lines': [{'color': 'R', 'order_qty': 1}]}]})
    a = db.save_order(o, [])
    assert db.delete_order(a)
    b = db.save_order(o, [])
    assert b != a and db.get_order(a) is None


def test_sheet_rows_missing_from_ai_output_are_reported():
    text = "item\tDescription\tBuyer PO\tQty\tValue\n\tA\t111\t5\t10\n\tB\t222\t6\t12\n\tC\tTBA-3\t7.5\t14\n\t\t\tTotal\t36\n"
    assert [r[0] for r in guard.table_rows(text)] == ['111', '222', 'TBA-3']
    d = {'pos': [{'po_number': '111', 'lines': [{'color': 'A', 'order_qty': 5}]}]}
    errs = guard.coverage_errors(d, text)
    assert any('Only 1 of 3' in e for e in errs) and any('222' in e for e in errs)


def test_rounded_lines_allow_rounding_drift_in_stated_total():
    d = {'pos': [{'po_number': 'P', 'stated_total_qty': 1300.9, 'lines': [
        {'color': 'A', 'order_qty': 650.45}, {'color': 'B', 'order_qty': 650.45}]}]}
    guard.soften(d)
    assert d['pos'][0]['stated_total_qty'] == 1301 and guard.arithmetic_errors(d) == []
    assert not [e for e in guard.text_errors(d, 'A 650.45\nB 650.45\nTotal 1300.9') if 'stated_total' in e]


def test_reply_with_trailing_or_repeated_json_is_parsed():
    from app import llm
    assert llm._json('{"pos": []}\n{"pos": [1]}') == {"pos": []}
    assert llm._json('here: {"a": {"b": 1}} done') == {"a": {"b": 1}}
