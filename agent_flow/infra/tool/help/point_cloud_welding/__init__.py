"""Offline implementation of Geng et al., RCIM 79 (2023), 102433.

Importing this package does not load scientific libraries or a robot SDK.
"""


def extract_weld_seams(**arguments):
    from .pipeline import extract_weld_seams as extract

    return extract(**arguments)
