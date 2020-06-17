import random
import string

def deep_update(target, update):
    if isinstance(target, dict):
        deep_update_dict(target, update)
    elif isinstance(target, list):
        deep_update_list(target, update)
    else:
        raise Exception('Cannot deep_update, %s not list or dict', target)
    return target

def deep_update_dict(target, update):
    if not isinstance(update, dict):
        raise Exception('Cannot deep_update dict with %s', type(update))
    for k, v in update.items():
        if k in target:
            if isinstance(target[k], dict) and isinstance(v, dict):
                deep_update_dict(target[k], v)
            elif isinstance(target[k], list) and isinstance(v, list):
                deep_update_list(target[k], v)
            else:
                target[k] = v
        else:
            target[k] = v

def deep_update_list(target, update):
    if not isinstance(update, list):
        raise Exception('Cannot deep_update list with %s', type(update))
    for i, v in enumerate(update):
        if i < len(target):
            if isinstance(target[i], dict) and isinstance(v, dict):
                deep_update_dict(target[i], v)
            elif isinstance(target[i], list) and isinstance(v, list):
                deep_update_list(target[i], v)
            else:
                target[i] = v
        else:
            target.append(v)

def random_string(length=8, character_set=string.ascii_lowercase + string.digits):
    return ''.join(random.choice(character_set) for i in range(length))

def k8s_ref(resource_dict):
    return dict(
        apiVersion = resource_dict['apiVersion'],
        kind = resource_dict['kind'],
        name = resource_dict['metadata']['name'],
        namespace = resource_dict['metadata']['namespace'],
        uid = resource_dict['metadata']['uid']
    )
