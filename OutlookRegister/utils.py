import random
import string
import secrets

def random_email(length=None):
    # 默认随机长度 11~17
    if length is None:
        length = random.randint(11, 17)

    first_char = random.choice(string.ascii_lowercase)

    other_chars = []
    for _ in range(length - 1):
        # 数字概率 10%
        if random.random() < 0.1:
            other_chars.append(random.choice(string.digits))
        else:
            other_chars.append(random.choice(string.ascii_lowercase))

    return first_char + ''.join(other_chars)

def generate_strong_password(length=None):
    if length is None:
        length = random.randint(10, 14)

    chars = string.ascii_letters + string.digits + "!@#$%^&*"

    while True:
        password = ''.join(secrets.choice(chars) for _ in range(length))

        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
            and any(c in "!@#$%^&*" for c in password)
        ):
            return password
