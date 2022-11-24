from collections.abc import Mapping
from copy import deepcopy
from typing import List

def deep_merge_lists(target, update, overwrite=True):
    for i, v in enumerate(update):
        if i < len(target):
            if isinstance(target[i], Mapping) and isinstance(v, Mapping):
                deep_merge_mappings(target[i], v, overwrite)
            elif isinstance(target[i], List) and isinstance(v, List):
                deep_merge_lists(target[i], v, overwrite)
            elif overwrite:
                target[i] = deepcopy(v)
        else:
            target.append(deepcopy(update))

def deep_merge_mappings(target, update, overwrite=True):
    for k, v in update.items():
        if k in target:
            if isinstance(target[k], Mapping) and isinstance(v, Mapping):
                deep_merge_mappings(target[k], v, overwrite)
            elif isinstance(target[k], List) and isinstance(v, List):
                deep_merge_lists(target[k], v, overwrite)
            elif overwrite:
                target[k] = deepcopy(v)
        else:
            target[k] = deepcopy(v)

def deep_merge(target, update, overwrite=True):
    if isinstance(target, Mapping) and isinstance(update, Mapping):
        deep_merge_mappings(target, update, overwrite)
    elif isinstance(target, List) and isinstance(update, List):
        deep_merge_lists(target, update, overwrite)
