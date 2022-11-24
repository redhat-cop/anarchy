import random
import string

def random_string(length=8, character_set=string.ascii_lowercase + string.digits):
    return ''.join(random.choice(character_set) for i in range(length))
