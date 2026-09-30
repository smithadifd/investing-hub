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
    assert "written" in capsys.readouterr().out


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
    assert "action, quantity" in err
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
    assert "pass --as-of" in capsys.readouterr().err
    assert _snapshots(db) == []


def test_bad_as_of_and_missing_file_exit_1(db, tmp_path):
    path = _write(tmp_path, "t.csv", TRANSACTIONS)
    assert _import(db, path, "--as-of", "01/02/2026") == 1
    assert _import(db, tmp_path / "gone.csv") == 1


def test_apply_needs_an_existing_database(tmp_path, capsys):
    path = _write(tmp_path, "t.csv", TRANSACTIONS)
    assert _import(tmp_path / "absent.db", path, "--apply") == 1
    assert "hub db init" in capsys.readouterr().err


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
