"""Empty boundary results only."""

from subprocess import CompletedProcess


def run():
    return CompletedProcess([], 0, "", "")


def entries():
    return []


def nothing():
    return None
