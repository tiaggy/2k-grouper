"""The web dashboard package. Its submodules import top-level sibling modules
(config, notion, notion_http, attendance, notionconfig, notionapprovals) that
live one directory up — this makes sure that directory is on sys.path
regardless of how the app is invoked (uvicorn, a direct script run, a
reloader subprocess), rather than relying on cwd being right by accident."""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
