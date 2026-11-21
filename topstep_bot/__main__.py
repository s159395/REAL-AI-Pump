"""Command line interface.

  python -m topstep_bot backtest  --data data/mnq_1m.csv
  python -m topstep_bot download  --symbol MNQ --days 120 --out data/mnq_1m.csv
  python -m topstep_bot accounts
  python -m topstep_bot paper
  python -m topstep_bot live --confirm
  python -m topstep_bot synthetic --out data/synthetic.csv   (software test data only)
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .config import credentials, load_config, load_dotenv


def _setup_logging(log_file: str | None, verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _client(cfg):
    from .broker.projectx import ProjectXClient

    user, key = credentials()
    return ProjectXClient(user, key, base_url=cfg["live"].get("base_url", "https://api.topstepx.com"))


def cmd_backtest(args, cfg) -> int:
    from .backtest import (
        combine_summary, format_combine, format_stats, instrument_from_config,
        run_combine_simulation, run_continuous, write_trades_csv,
    )
    from .data import load_csv

    bars = load_csv(args.data, tz=args.tz, bar_time=args.bar_time)
    if not bars:
        print("No bars loaded.")
        return 1
    inst = instrument_from_config(cfg)
    print(f"Loaded {len(bars):,} bars {bars[0].ts:%Y-%m-%d} -> {bars[-1].ts:%Y-%m-%d} | {inst.symbol} | "
          f"strategies: {', '.join(cfg['strategies']['enabled'])}")
    res = run_continuous(cfg, bars, inst)
    print()
    print(format_stats(res.stats))
    if args.trades_out:
        write_trades_csv(res.trades, args.trades_out)
        print(f"\nTrades written to {args.trades_out}")
    if args.verbose:
        print("\nEvent log:")
        for ev in res.engine.events:
            if ev.kind != "skip":
                print(f"  {ev.ts:%Y-%m-%d %H:%M} {ev.kind:7s} {ev.message}")
    if not args.no_combine:
        max_days = int(cfg["backtest"].get("combine_max_days", 30))

        def progress(i, n, r):
            if sys.stdout.isatty():
                sys.stdout.write(f"\r  simulating combine attempts... {i}/{n}")
                sys.stdout.flush()

        results = run_combine_simulation(cfg, bars, inst, step=args.combine_step, max_days=max_days, progress=progress)
        if sys.stdout.isatty():
            print("\r" + " " * 60 + "\r", end="")
        print()
        print(format_combine(combine_summary(results), max_days))
    print(
        "\nReminder: past performance does not guarantee future results. "
        "Validate on out-of-sample data and paper trade before going live."
    )
    return 0


def cmd_synthetic(args, cfg) -> int:
    from .data import generate_synthetic, save_csv

    bars = generate_synthetic(date.fromisoformat(args.start), args.days, seed=args.seed)
    n = save_csv(bars, args.out)
    print(f"Wrote {n:,} SYNTHETIC bars to {args.out} (random walk - for testing the software only)")
    return 0


def cmd_download(args, cfg) -> int:
    from .data import save_csv

    client = _client(cfg)
    client.login()
    if args.contract_id:
        cid = args.contract_id
    else:
        c = client.resolve_contract(args.symbol or cfg["instrument"]["symbol"])
        cid = c["id"]
        print(f"Using contract {cid} ({c.get('description', '')})")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    bars = client.retrieve_bars_range(cid, start, end, chunk_days=args.chunk_days)
    n = save_csv(bars, args.out)
    print(f"Wrote {n:,} one-minute bars to {args.out}")
    if n and (bars[0].ts - start) > timedelta(days=3):
        print(f"Note: data starts at {bars[0].ts:%Y-%m-%d}; the API may only serve this contract's history "
              "from when it became active. Pass --contract-id for older contracts and join the CSVs.")
    return 0


def cmd_accounts(args, cfg) -> int:
    client = _client(cfg)
    client.login()
    accts = client.search_accounts(only_active=not args.all)
    if not accts:
        print("No accounts found.")
    for a in accts:
        print(f"id={a.get('id'):<10} name={a.get('name'):<30} balance={a.get('balance'):>12} "
              f"canTrade={a.get('canTrade')} simulated={a.get('simulated')}")
    return 0


def cmd_run(args, cfg, mode: str) -> int:
    from .live import Runner

    if mode == "live" and not args.confirm:
        print("Live mode places REAL orders on your TopstepX account.\n"
              "Run it again with --confirm once you have backtested and paper traded this configuration.")
        return 2
    runner = Runner(cfg, _client(cfg), mode=mode, flatten_on_exit=not args.keep_position_on_exit)
    runner.run()
    return 0


def cmd_status(args, cfg) -> int:
    from .risk import CombineTracker

    path = cfg["live"].get("state_file", "state/combine_state.json")
    if args.paper:
        path = path.replace(".json", "_paper.json")
    st = CombineTracker.load_state(path)
    if st is None:
        print(f"No state file at {path}")
        return 1
    t = CombineTracker(cfg["combine"], st)
    print(f"Status     : {t.status.upper()} {t.s.status_reason}")
    print(f"Balance    : {t.balance:,.2f}  (profit {t.total_profit:+,.2f} / target {t.target:,.2f})")
    print(f"MLL        : {t.mll:,.2f}  (room {t.balance - t.mll:,.2f})")
    print(f"Best day   : {t.best_day:,.2f}  consistency ok: {t.consistency_ok()}")
    print(f"Days traded: {t.trading_days}")
    for d, p in sorted(t.s.daily_pnl.items()):
        print(f"  {d}: {p:+,.2f}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="topstep_bot", description="Topstep 50K Combine day-trading bot")
    ap.add_argument("--config", default="config.yaml", help="YAML config file (default: config.yaml)")
    ap.add_argument("--env", default=".env", help="file with TOPSTEPX_USERNAME / TOPSTEPX_API_KEY")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backtest", help="backtest + 50K Combine simulation on a CSV of 1-minute bars")
    b.add_argument("--data", required=True)
    b.add_argument("--tz", default="UTC", help="timezone of timestamps without an offset (default UTC)")
    b.add_argument("--bar-time", choices=["open", "close"], default="open",
                   help="whether CSV timestamps mark bar open (default) or bar close")
    b.add_argument("--combine-step", type=int, default=1, help="start a simulated combine every N days")
    b.add_argument("--no-combine", action="store_true", help="skip the combine simulation")
    b.add_argument("--trades-out", default=None, help="write every trade to this CSV")

    s = sub.add_parser("synthetic", help="write random-walk test data (NOT real market data)")
    s.add_argument("--out", default="data/synthetic_mnq_1m.csv")
    s.add_argument("--days", type=int, default=120)
    s.add_argument("--start", default="2026-01-05")
    s.add_argument("--seed", type=int, default=7)

    d = sub.add_parser("download", help="download 1-minute bars from TopstepX to CSV")
    d.add_argument("--symbol", default=None)
    d.add_argument("--contract-id", default=None)
    d.add_argument("--days", type=int, default=90)
    d.add_argument("--chunk-days", type=int, default=5)
    d.add_argument("--out", required=True)

    a = sub.add_parser("accounts", help="list your TopstepX accounts (to find the account id)")
    a.add_argument("--all", action="store_true", help="include inactive accounts")

    for name, helptext in (("paper", "live data, simulated fills (no orders sent)"),
                           ("live", "live data, REAL orders on your account")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--keep-position-on-exit", action="store_true",
                       help="don't flatten on Ctrl+C (server-side brackets stay active)")
        if name == "live":
            p.add_argument("--confirm", action="store_true", help="required: confirms you want real orders")

    st = sub.add_parser("status", help="show the saved Combine state")
    st.add_argument("--paper", action="store_true")

    args = ap.parse_args(argv)
    load_dotenv(args.env)
    cfg_path = args.config if Path(args.config).exists() else None
    if cfg_path is None and args.config != "config.yaml":
        print(f"config file not found: {args.config}")
        return 1
    cfg = load_config(cfg_path)
    log_file = cfg["live"].get("log_file") if args.cmd in ("paper", "live") else None
    _setup_logging(log_file, args.verbose)

    if args.cmd == "backtest":
        return cmd_backtest(args, cfg)
    if args.cmd == "synthetic":
        return cmd_synthetic(args, cfg)
    if args.cmd == "download":
        return cmd_download(args, cfg)
    if args.cmd == "accounts":
        return cmd_accounts(args, cfg)
    if args.cmd in ("paper", "live"):
        return cmd_run(args, cfg, args.cmd)
    if args.cmd == "status":
        return cmd_status(args, cfg)
    return 1


def _entry() -> int:
    try:
        return main()
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(_entry())
