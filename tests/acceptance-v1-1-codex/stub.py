"""Empty boundaries only; no command- or criterion-dependent behaviour."""
from subprocess import CompletedProcess


def run(*args, **kwargs):
    return CompletedProcess([], 0, '', '')


def text(*args, **kwargs):
    return ''


def entries(*args, **kwargs):
    return []


def snapshot(*args, **kwargs):
    return {}


def nothing(*args, **kwargs):
    return None


def v100_cli(*args, **kwargs):
    return None


def binary(*args, **kwargs):
    return b''


def seconds(*args, **kwargs):
    return 0


def signin_boundary(*args, **kwargs):
    return None


def signin_answer(*args, **kwargs):
    return None


def signin_calls(*args, **kwargs):
    return []


def signin_snapshot(*args, **kwargs):
    return {}


def signin_alerts(*args, **kwargs):
    return []


def signin_profile(*args, **kwargs):
    return None


def result_json(*args, **kwargs):
    return {}


def signin_relay_wrapper(*args, **kwargs):
    return None


def signin_relay_environments(*args, **kwargs):
    return []


def second_state_root(*args, **kwargs):
    return None
