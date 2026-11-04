"""Test-suite defaults.

The friend-chat identity guard ships disabled so an unverified live page shape
cannot block sends; the suite exercises the guarded behaviour, so turn it on
here. Tests for the disabled path override the variable explicitly.
"""
import os

os.environ.setdefault("SPARKFLOW_FRIEND_GUARD", "on")
