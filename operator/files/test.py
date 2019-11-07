from anarchyutil import deep_update

x = {
    'name': 'foo',
    'spec': {
       'name': 'fooi',
       'items': [
           { 'name': 'fool'}
       ]
    }
}

update = {
    'namespace': 'bar',
    'spec': {
        'items': [
            { 'image': 'woo' }
        ]
    }
}

deep_update(x, update)

print(x)
