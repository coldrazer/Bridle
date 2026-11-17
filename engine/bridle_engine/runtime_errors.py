"""Exceptions shared by the runtime and its optional feature modules."""


class FeatureUnavailable(Exception):
    """An optional capability (vision, speech, embeddings) isn't installed."""
