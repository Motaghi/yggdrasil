def present(value):
    assert value is not None, 'The signed example requires a nonempty observable record'
    return value


def merge_parents(value):
    assert len(value) == 2, 'The signed example requires a merge commit with two parents'
    return value
