"""Stand-in for RAGFlow's api.utils.crypt in tests: marks the password instead of RSA."""


def crypt(password):
    return "ENC:" + password
