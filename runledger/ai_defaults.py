"""Model defaults shared by the CLI and optional AI features."""
import os

DEFAULT_AI_MODEL = os.environ.get("RUNLEDGER_SUMMARY_MODEL", "claude-haiku-5-5")
DEFAULT_REVIEW_MODEL = "claude-sonnet-5-5"
