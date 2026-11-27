"""Print a password hash for ELI_WEB_PASSWORD_HASH: ``python -m web.hash_password``.

The password is read without echo and never stored or printed.
"""
import getpass
import secrets
import sys

from werkzeug.security import generate_password_hash

MIN_LENGTH = 10


def main():
    password = getpass.getpass('New dashboard password: ')
    if len(password) < MIN_LENGTH:
        print(f'Use at least {MIN_LENGTH} characters.', file=sys.stderr)
        return 1
    if getpass.getpass('Repeat password: ') != password:
        print('Passwords do not match.', file=sys.stderr)
        return 1
    print('\nELI_WEB_PASSWORD_HASH=' + generate_password_hash(password))
    print('Suggested ELI_WEB_SECRET_KEY=' + secrets.token_urlsafe(48))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
