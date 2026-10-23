"""Arguments shared by the evaluation supervisor and its worker."""

import argparse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live chatbot evaluation scenarios.")
    parser.add_argument(
        "--only", default="1,2,3,4,5,6,7,8,9", help="comma list of scenario numbers"
    )
    parser.add_argument("--state-dir", help="reuse and preserve a specific state directory")
    parser.add_argument(
        "--keep-state", action="store_true", help="retain a fresh run for inspection"
    )
    parser.add_argument("--turns", type=int, default=500, help="synthetic Slack history length")
    parser.add_argument("--quiet", action="store_true", help="don't print the conversation")
    args = parser.parse_args(argv)
    selected = [key.strip() for key in args.only.split(",") if key.strip()]
    if not selected or any(key not in "123456789" or len(key) != 1 for key in selected):
        parser.error("--only must contain scenario numbers 1 through 9")
    if any(key in selected for key in ("2", "3", "6", "7")) and args.turns < 500:
        parser.error("Long-history acceptance scenarios require --turns >= 500")
    return args
