"""Moved to app.services.wealth.policy; this name stays an alias of that module.

The alias (not a star import) keeps one module object, so constants patched in
tests and private helpers resolve exactly as before.
"""
import sys

from app.services.wealth import policy as _policy

sys.modules[__name__] = _policy
