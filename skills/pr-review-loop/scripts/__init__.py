"""Vendor-neutral pull-request review skill implementation."""

from . import cli

# Keep package-level test imports stable while the CLI module is renamed by responsibility.
loopr = cli

__all__ = ["cli", "loopr"]
