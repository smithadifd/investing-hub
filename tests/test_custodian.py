import csv
import sqlite3
from pathlib import Path

import pytest

from hub import custodian, store
from hub.cli import main

TRANSACTIONS = """Transactions for account TESTACCT
Date,Action,Symbol,Quantity,Price,Fees & Comm,Amount
01/02/2026,Buy,TEST1,10,5.00,0.00,-50.00
01/09/2026 as of 01/08/2026,Sell,TEST2,5,4.00,0.00,20.00
"""

POSITIONS = """Positions as of a test date
Ticker,Name,Shares,Last Price,Market Value,Cost Basis,% of Acct
TEST1,Test One Corp,10,5.00,50.00,40.00,50%
TEST2,Test Two Corp,20,2.50,50.00,45.00,50%
"""


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "hub.db"
    assert main(["db", "init", "--db", str(path)]) == 0
    return path


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


def _import(db, path, *extra, kind="transactions", custodian_name="testco"):
    argv = ["custodian", "import", str(path), "--custodian", custodian_name, "--kind", kind]
    return main(argv + ["--db", str(db)] + list(extra))


def _snapshots(db):
    conn = store.connect(db)
    try:
        return store.list_custodian_snapshots(conn)
    finally:
        conn.close()


# --- parse: the module interface -------------------------------------------------------


def test_parse_skips_preamble_and_maps_transactions():
    parsed = custodian.parse(TRANSACTIONS, "transactions")
    assert parsed.header_row == 1
    assert parsed.headers[0] == "Date"
    assert parsed.mapping == {
        "date": 0,
        "action": 1,
        "symbol": 2,
        "quantity": 3,
        "price": 4,
        "fees": 5,
        "amount": 6,
    }
    assert len(parsed.rows) == 2
    assert parsed.missing == []


@pytest.mark.parametrize(
    ("header", "field"),
    [
        ("Trade Date", "date"),
        ("Transaction Type", "action"),
        ("Ticker", "symbol"),
        ("Qty", "quantity"),
        ("Execution Price", "price"),
        ("FEES_AND_COMM", "fees"),
        ("Net Amount", "amount"),
        ("Reference Number", "external_id"),
    ],
)
def test_transactions_synonym_variant_per_family(header, field):
    base = ["Date", "Action", "Symbol", "Quantity", "Price", "Fees", "Amount", "Id"]
    names = {
        "date": 0,
        "action": 1,
        "symbol": 2,
        "quantity": 3,
        "price": 4,
        "fees": 5,
        "amount": 6,
        "external_id": 7,
    }
    base[names[field]] = header
    text = ",".join(base) + "\n" + ",".join(["x"] * 8) + "\n"
    assert custodian.parse(text, "transactions").mapping[field] == names[field]


@pytest.mark.parametrize(
    ("header", "field"),
    [
        ("Ticker", "symbol"),
        ("Security Name", "description"),
        ("Shares", "quantity"),
        ("Last Price", "price"),
        ("Mkt Val", "market_value"),
        ("Cost Basis Total", "cost_basis"),
        ("% of Acct", "account_percent"),
    ],
)
def test_positions_synonym_variant_per_family(header, field):
    names = {
        "symbol": 0,
        "description": 1,
        "quantity": 2,
        "price": 3,
        "market_value": 4,
        "cost_basis": 5,
        "account_percent": 6,
    }
    base = ["Symbol", "Description", "Quantity", "Price", "Market Value", "Cost Basis", "Percent"]
    base[names[field]] = header
    text = ",".join(base) + "\n" + ",".join(["x"] * 7) + "\n"
    assert custodian.parse(text, "positions").mapping[field] == names[field]


def test_parse_map_override_wins_over_synonyms():
    text = "Day,Action,Symbol,Quantity\n01/02/2026,Buy,TEST1,1\n"
    assert custodian.parse(text, "transactions").missing == ["date"]
    parsed = custodian.parse(text, "transactions", {"day": "date"})
    assert parsed.missing == []
    assert parsed.mapping["date"] == 0


def test_parse_map_rejects_unknown_field_and_absent_header():
    text = "Date,Action,Symbol,Quantity\n01/02/2026,Buy,TEST1,1\n"
    with pytest.raises(custodian.CustodianError, match="unknown field"):
        custodian.parse(text, "transactions", {"Date": "colour"})
    with pytest.raises(custodian.CustodianError, match="not found in the file"):
        custodian.parse(text, "transactions", {"Nope": "price"})


def test_no_recognized_header_reports_first_nonempty_cells_and_map_form():
    text = "\nCustomTicker,CustomUnits\nTEST1,10\n"
    with pytest.raises(custodian.CustodianError) as caught:
        custodian.parse(text, "positions")
    message = str(caught.value)
    assert "headers found: CustomTicker, CustomUnits" in message
    assert "--map 'header=field'" in message


def test_newest_date_uses_first_date_of_an_as_of_cell():
    parsed = custodian.parse(TRANSACTIONS, "transactions")
    assert parsed.dates == ["2026-01-02", "2026-01-09"]
    assert parsed.newest_date == "2026-01-09"


def test_source_ref_relative_inside_root_else_as_given(tmp_path):
    inside = tmp_path / "drop" / "positions.csv"
    inside.parent.mkdir()
    inside.write_text("x")
    assert custodian.source_ref(inside, tmp_path) == "drop/positions.csv"
    assert custodian.source_ref(Path("/elsewhere/file.csv"), tmp_path) == "/elsewhere/file.csv"


# --- CLI: import ----------------------------------------------------------------------


def test_dry_run_reports_mapping_and_writes_nothing(db, tmp_path, capsys):
    path = _write(tmp_path, "t.csv", TRANSACTIONS)
    assert _import(db, path) == 0
    assert _snapshots(db) == []
    out = capsys.readouterr().out
    assert "Fees & Comm -> fees" in out
    assert "rows: 2" in out
    assert "as_of: 2026-01-09" in out
    assert "dry run: nothing written" in out


def test_dry_run_needs_no_database(tmp_path, capsys):
    path = _write(tmp_path, "t.csv", TRANSACTIONS)
    assert _import(tmp_path / "absent.db", path) == 0
    assert not (tmp_path / "absent.db").exists()


def test_apply_writes_one_snapshot_with_raw_and_newest_date(db, tmp_path, capsys):
    path = _write(tmp_path, "t.csv", TRANSACTIONS)
    assert _import(db, path, "--apply") == 0
    (snap,) = _snapshots(db)
    assert snap["custodian"] == "testco"
    assert snap["kind"] == "transactions"
    assert snap["as_of"] == "2026-01-09"
    assert snap["source_ref"] == str(path)
    assert snap["raw"] == TRANSACTIONS
    out = capsys.readouterr().out
    assert "Date -> date" in out
    assert "written" in out


def test_apply_positions_with_explicit_as_of(db, tmp_path):
    path = _write(tmp_path, "p.csv", POSITIONS)
    assert _import(db, path, "--apply", "--as-of", "2026-02-01", kind="positions") == 0
    (snap,) = _snapshots(db)
    assert (snap["kind"], snap["as_of"]) == ("positions", "2026-02-01")


def test_second_apply_is_a_noop(db, tmp_path, capsys):
    path = _write(tmp_path, "t.csv", TRANSACTIONS)
    assert _import(db, path, "--apply") == 0
    capsys.readouterr()
    assert _import(db, path, "--apply") == 0
    assert "no-op" in capsys.readouterr().out
    assert len(_snapshots(db)) == 1


def test_changed_file_same_key_is_left_as_is(db, tmp_path, capsys):
    path = _write(tmp_path, "t.csv", TRANSACTIONS)
    assert _import(db, path, "--apply") == 0
    path.write_text(TRANSACTIONS + "01/09/2026,Buy,TEST1,1,1.00,0.00,-1.00\n")
    capsys.readouterr()
    assert _import(db, path, "--apply") == 0
    assert "file content differs" in capsys.readouterr().out
    assert _snapshots(db)[0]["raw"] == TRANSACTIONS


def test_different_as_of_is_a_new_snapshot(db, tmp_path):
    path = _write(tmp_path, "p.csv", POSITIONS)
    assert _import(db, path, "--apply", "--as-of", "2026-02-01", kind="positions") == 0
    assert _import(db, path, "--apply", "--as-of", "2026-03-01", kind="positions") == 0
    assert len(_snapshots(db)) == 2


def test_transactions_missing_required_header_exits_1_with_map_form(db, tmp_path, capsys):
    path = _write(tmp_path, "t.csv", "Date,Symbol,Price\n01/02/2026,TEST1,5\n")
    assert _import(db, path) == 1
    err = capsys.readouterr().err
    assert "missing required transactions column(s): action, quantity" in err
    assert "headers found: Date, Symbol, Price" in err
    assert "--map '<header>=action'" in err
    assert "--map '<header>=quantity'" in err


def test_positions_need_only_symbol_and_quantity(db, tmp_path):
    ok = _write(tmp_path, "ok.csv", "Symbol,Quantity\nTEST1,10\n")
    assert _import(db, ok, "--as-of", "2026-02-01", kind="positions") == 0
    bad = _write(tmp_path, "bad.csv", "Symbol,Price\nTEST1,10\n")
    assert _import(db, bad, "--as-of", "2026-02-01", kind="positions") == 1


def test_same_file_fails_as_positions_when_it_lacks_positions_columns(db, tmp_path):
    path = _write(tmp_path, "t.csv", "Date,Action,Symbol\n01/02/2026,Buy,TEST1\n")
    assert _import(db, path, kind="transactions") == 1  # no quantity
    assert _import(db, path, "--as-of", "2026-02-01", kind="positions") == 1  # no quantity


def test_map_override_on_the_cli(db, tmp_path):
    path = _write(tmp_path, "t.csv", "Day,Action,Symbol,Quantity\n01/02/2026,Buy,TEST1,1\n")
    assert _import(db, path) == 1
    assert _import(db, path, "--map", "Day=date", "--apply") == 0
    assert _snapshots(db)[0]["as_of"] == "2026-01-02"


def test_pdf_is_refused_with_one_line(db, tmp_path, capsys):
    path = _write(tmp_path, "statement.pdf", "%PDF-1.4")
    assert _import(db, path, "--apply") == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "only .csv" in err
    assert _snapshots(db) == []


def test_positions_without_date_needs_as_of_to_apply(db, tmp_path, capsys):
    path = _write(tmp_path, "p.csv", POSITIONS)
    assert _import(db, path, kind="positions") == 0  # dry run still reports
    assert "pass --as-of" in capsys.readouterr().out
    assert _import(db, path, "--apply", kind="positions") == 1
    out, err = capsys.readouterr()
    assert "pass --as-of" in err
    assert out == ""
    assert _snapshots(db) == []


def test_bad_as_of_and_missing_file_exit_1(db, tmp_path):
    path = _write(tmp_path, "t.csv", TRANSACTIONS)
    assert _import(db, path, "--as-of", "01/02/2026") == 1
    assert _import(db, tmp_path / "gone.csv") == 1


def test_apply_needs_an_existing_database(tmp_path, capsys):
    path = _write(tmp_path, "t.csv", TRANSACTIONS)
    assert _import(tmp_path / "absent.db", path, "--apply") == 1
    out, err = capsys.readouterr()
    assert "hub db init" in err
    assert out == ""


# --- CLI: list ------------------------------------------------------------------------


def test_list_shows_snapshots_with_row_count_from_raw(db, tmp_path, capsys):
    _import(db, _write(tmp_path, "t.csv", TRANSACTIONS), "--apply")
    _import(
        db,
        _write(tmp_path, "p.csv", POSITIONS),
        "--apply",
        "--as-of",
        "2026-02-01",
        kind="positions",
        custodian_name="otherco",
    )
    capsys.readouterr()
    assert main(["custodian", "list", "--db", str(db)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["custodian", "kind", "as_of", "imported_at", "rows"]
    by_kind = {line.split()[1]: line.split() for line in lines[1:]}
    assert by_kind["transactions"][0] == "testco"
    assert by_kind["transactions"][-1] == "2"
    assert by_kind["positions"][0] == "otherco"
    assert by_kind["positions"][-1] == "2"


def test_list_empty_prints_only_headers(db, capsys):
    assert main(["custodian", "list", "--db", str(db)]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_list_uses_persisted_map_overrides_for_custom_headers(db, tmp_path, capsys):
    custom_positions = "CustomTicker,CustomUnits\nTEST1,10\nTEST2,20\n"
    path = _write(tmp_path, "custom.csv", custom_positions)
    assert (
        _import(
            db,
            path,
            "--map",
            "CustomTicker=symbol",
            "--map",
            "CustomUnits=quantity",
            "--as-of",
            "2026-02-01",
            "--apply",
            kind="positions",
        )
        == 0
    )
    capsys.readouterr()
    assert main(["custodian", "list", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[1].split()[-1] == "2"


def test_migration_0002_applies_to_db_at_0001(tmp_path):
    db = tmp_path / "hub.db"
    conn = store.connect(db)
    migrations_0001 = [m for m in store.load_migrations() if m.version == 1]
    assert len(migrations_0001) == 1
    store.migrate(conn, migrations_0001)
    columns_before = {row["name"] for row in conn.execute("PRAGMA table_info(custodian_snapshots)")}
    assert "mapping" not in columns_before
    applied = store.migrate(conn)
    assert applied == ["0002_custodian_mapping.sql"]
    columns_after = {row["name"] for row in conn.execute("PRAGMA table_info(custodian_snapshots)")}
    assert "mapping" in columns_after
    snap_id, created = store.insert_custodian_snapshot(
        conn,
        custodian="testco",
        kind="positions",
        as_of="2026-02-01",
        source_ref="drop/positions.csv",
        raw="CustomTicker,CustomUnits\nTEST1,10\n",
        mapping='{"customticker": "symbol"}',
    )
    assert created is True
    snaps = store.list_custodian_snapshots(conn)
    assert len(snaps) == 1
    assert snaps[0]["mapping"] == '{"customticker": "symbol"}'
    conn.close()


def test_apply_at_migration_0001_has_no_stdout_and_names_migrate_command(tmp_path, capsys):
    db = tmp_path / "hub.db"
    conn = store.connect(db)
    migrations_0001 = [m for m in store.load_migrations() if m.version == 1]
    store.migrate(conn, migrations_0001)
    conn.close()
    path = _write(tmp_path, "p.csv", POSITIONS)

    assert (
        _import(
            db,
            path,
            "--apply",
            "--as-of",
            "2026-02-01",
            kind="positions",
        )
        == 1
    )
    out, err = capsys.readouterr()
    assert out == ""
    assert "hub db migrate" in err
    assert len(err.strip().splitlines()) == 1


def test_map_duplicate_normalized_headers_rejected(db, tmp_path, capsys):
    path = _write(tmp_path, "onecol.csv", "Holding\nTEST1\n")
    assert (
        _import(
            db,
            path,
            "--map",
            "Holding=symbol",
            "--map",
            "holding=quantity",
            kind="positions",
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "duplicate normalized override header" in err
    assert len(err.strip().splitlines()) == 1


def test_map_duplicate_target_field_rejected(db, tmp_path, capsys):
    path = _write(tmp_path, "twocol.csv", "ColA,ColB\nTEST1,10\n")
    assert (
        _import(
            db,
            path,
            "--map",
            "ColA=symbol",
            "--map",
            "ColB=symbol",
            kind="positions",
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "duplicate target field in --map" in err
    assert len(err.strip().splitlines()) == 1


def test_column_mapped_to_multiple_fields_rejected(monkeypatch):
    text = "Ticker,Qty\nTEST1,10\n"
    monkeypatch.setattr(
        custodian, "_map_row", lambda kind, row, overrides: ({"symbol": 0, "quantity": 0}, [])
    )
    with pytest.raises(custodian.CustodianError, match="supplies more than one field"):
        custodian.parse(text, "positions")


def test_csv_error_wrapped_in_custodian_error(db, tmp_path, capsys):
    old_limit = csv.field_size_limit(32)
    try:
        field = "X" * 64
        path = _write(
            tmp_path, "large.csv", f"Date,Action,Symbol,Quantity\n01/02/2026,Buy,{field},1\n"
        )
        assert _import(db, path) == 1
        err = capsys.readouterr().err
        assert "CSV parse error" in err
        assert len(err.strip().splitlines()) == 1
    finally:
        csv.field_size_limit(old_limit)


def test_iso8601_datetime_cells_parsed_and_newest_date_selected():
    text = (
        "Date,Action,Symbol,Quantity\n"
        "2026-01-02T15:30:00Z,Buy,TEST1,10\n"
        "2026-01-09T18:45:00.123456-05:00,Sell,TEST2,5\n"
        "2026-01-05T00:00:00+02:00,Buy,TEST3,1\n"
    )
    parsed = custodian.parse(text, "transactions")
    assert parsed.dates == ["2026-01-02", "2026-01-09", "2026-01-05"]
    assert parsed.newest_date == "2026-01-09"


def test_summary_rows_ignored_without_excluding_ticker_prefixes():
    text = (
        "Ticker,Shares\n"
        "TEST1,10\n"
        "TOTL,20\n"
        "Subtotal,30\n"
        "Grand Total,30\n"
        "Grand Total Holdings,30\n"
        "Summary,30\n"
        "Cash & Cash Investments Total,30\n"
        "Total,30\n"
    )
    parsed = custodian.parse(text, "positions", {"Shares": "quantity"})
    assert [row[0] for row in parsed.rows] == ["TEST1", "TOTL"]
    assert custodian.count_rows(text, "positions", {"Shares": "quantity"}) == 2

    text_nums = 'Ticker,Shares\nTEST1,(15.5)\nTEST2,"1,250"\nTEST3,-50\nTotal,30\n'
    parsed_nums = custodian.parse(text_nums, "positions", {"Shares": "quantity"})
    assert len(parsed_nums.rows) == 3


def test_summary_labels_require_a_token_boundary():
    text = (
        "Ticker,Shares\n"
        "TOTAL1,10\n"
        "SUMMARYCO,20\n"
        "SUBTOTALITY,30\n"
        "TOTL,40\n"
        "Total,50\n"
        "Grand Total,60\n"
        "Account Total:,70\n"
        "Subtotal:,80\n"
        "Cash Total,90\n"
    )
    parsed = custodian.parse(text, "positions", {"Shares": "quantity"})
    assert [row[0] for row in parsed.rows] == ["TOTAL1", "SUMMARYCO", "SUBTOTALITY", "TOTL"]


def test_non_numeric_quantity_row_ignored():
    text = "Ticker,Shares\nTEST1,10\nNote,see footnote\nTEST2,20\n"
    parsed = custodian.parse(text, "positions", {"Shares": "quantity"})
    assert [row[0] for row in parsed.rows] == ["TEST1", "TEST2"]


def test_apply_write_failure_has_empty_stdout(db, tmp_path, capsys, monkeypatch):
    path = _write(tmp_path, "p.csv", POSITIONS)

    def fail_insert(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic write failure")

    monkeypatch.setattr(store, "insert_custodian_snapshot", fail_insert)
    assert (
        _import(
            db,
            path,
            "--apply",
            "--as-of",
            "2026-02-01",
            kind="positions",
        )
        == 1
    )
    out, err = capsys.readouterr()
    assert out == ""
    assert "synthetic write failure" in err
    assert len(err.strip().splitlines()) == 1


def test_apply_fails_before_printing_mapping_report(tmp_path, capsys):
    path = _write(tmp_path, "p.csv", POSITIONS)
    assert (
        _import(
            tmp_path / "absent.db",
            path,
            "--apply",
            "--as-of",
            "2026-02-01",
            kind="positions",
        )
        == 1
    )
    out, err = capsys.readouterr()
    assert "hub db init" in err
    assert out == ""

    db = tmp_path / "hub.db"
    assert main(["db", "init", "--db", str(db)]) == 0
    capsys.readouterr()
    assert _import(db, path, "--apply", kind="positions") == 1
    out, err = capsys.readouterr()
    assert "no date found in the file; pass --as-of YYYY-MM-DD" in err
    assert out == ""
