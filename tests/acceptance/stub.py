"""Empty observable boundaries; deliberately independent of commands and ACs."""

from subprocess import CompletedProcess


def run(*args, **kwargs):
    return CompletedProcess([], 0, "", "")


def text(*args, **kwargs):
    return ""


def entries(*args, **kwargs):
    return []


def snapshot(*args, **kwargs):
    return {}


def nothing(*args, **kwargs):
    return None


def binary(*args, **kwargs):
    return b""
