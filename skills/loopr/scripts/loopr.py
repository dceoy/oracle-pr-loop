#!/usr/bin/env python3
"""Vendor-neutral loopr command entrypoint."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
from typing import Sequence
from models import EXIT_PRECONDITION,LooprError
from process import CommandRunner
from review import execute_review
def parser()->argparse.ArgumentParser:
 root=argparse.ArgumentParser(description="Review one exact GitHub pull-request head through Oracle/ChatGPT.");sub=root.add_subparsers(dest="command",required=True);review=sub.add_parser("review",help="review and post one exact pull-request snapshot")
 review.add_argument("--pr",required=True,help="positive PR number or canonical GitHub pull URL");review.add_argument("--repo-dir",default=".",help="local checkout used for immutable Git object reads");review.add_argument("--artifacts-dir",default=".pr-loopr",help="private artifact directory");review.add_argument("--oracle-thinking-time",choices=("light","standard","extended","heavy"),default="heavy");return root
def main(argv:Sequence[str]|None=None)->int:
 args=parser().parse_args(argv);runner=CommandRunner()
 try:
  result=execute_review(pr_value=args.pr,repo_dir=Path(args.repo_dir),artifacts_dir=Path(args.artifacts_dir),thinking_time=args.oracle_thinking_time,runner=runner);sys.stdout.write(json.dumps(result.as_json(),sort_keys=True,separators=(",",":"))+"\n");return 0
 except LooprError as exc:
  error={"schema_version":1,"command":"review","error":{"category":exc.category,"message":runner.redact(str(exc))}};sys.stdout.write(json.dumps(error,sort_keys=True,separators=(",",":"))+"\n");sys.stderr.write(f"loopr review: {runner.redact(str(exc))}\n");return exc.code
 except KeyboardInterrupt:
  error={"schema_version":1,"command":"review","error":{"category":"interrupted","message":"interrupted; failed closed"}};sys.stdout.write(json.dumps(error,sort_keys=True,separators=(",",":"))+"\n");sys.stderr.write("loopr review: interrupted; failed closed\n");return EXIT_PRECONDITION
if __name__=="__main__":raise SystemExit(main())
