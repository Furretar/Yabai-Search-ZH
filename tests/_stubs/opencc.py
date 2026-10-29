"""Test-only stand-in for OpenCC (identity conversion plus a few fixture words)."""


class OpenCC:
    def __init__(self, *_args):
        pass

    def convert(self, text):
        return {"會": "会"}.get(text, text)
