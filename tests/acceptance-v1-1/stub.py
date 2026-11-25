"""Deliberately empty boundary responses."""
from subprocess import CompletedProcess


def run():
    return CompletedProcess([], 0, '', '')


def text():
    return ''


def entries():
    return []


def nothing():
    return None
