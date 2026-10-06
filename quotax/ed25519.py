"""Ed25519 signatures (RFC 8032, section 5.1) and OpenSSH key files, for Ollama's request signing.

The standard library has no Ed25519. This follows the RFC's reference algorithm with extended
coordinates; Quotax signs a handful of short messages per refresh, so speed is not a concern.
"""

import base64
import hashlib
import os
import struct

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int, sign: int) -> int:
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if x & 1 != sign:
        x = _P - x
    return x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_BASE = (_GX, _GY, 1, _GX * _GY % _P)


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _multiply(scalar: int, point):
    result = (0, 1, 1, 0)
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _compress(point) -> bytes:
    inverse = pow(point[2], _P - 2, _P)
    x, y = point[0] * inverse % _P, point[1] * inverse % _P
    return (y | (x & 1) << 255).to_bytes(32, "little")


def _expand(seed: bytes) -> tuple[int, bytes]:
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def public_key(seed: bytes) -> bytes:
    scalar, _prefix = _expand(seed)
    return _compress(_multiply(scalar, _BASE))


def sign(seed: bytes, message: bytes) -> bytes:
    scalar, prefix = _expand(seed)
    public = _compress(_multiply(scalar, _BASE))
    nonce = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _L
    commitment = _compress(_multiply(nonce, _BASE))
    challenge = int.from_bytes(hashlib.sha512(commitment + public + message).digest(), "little") % _L
    return commitment + ((nonce + challenge * scalar) % _L).to_bytes(32, "little")


# OpenSSH key files ("openssh-key-v1"), the format Ollama and ssh-keygen use for id_ed25519.

_MAGIC = b"openssh-key-v1\x00"
_KEY_TYPE = b"ssh-ed25519"
_PEM_BEGIN = "-----BEGIN OPENSSH PRIVATE KEY-----"
_PEM_END = "-----END OPENSSH PRIVATE KEY-----"


def _string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def _read_string(data: bytes, offset: int) -> tuple[bytes, int]:
    (length,) = struct.unpack_from(">I", data, offset)
    start = offset + 4
    if start + length > len(data):
        raise ValueError("truncated key")
    return data[start : start + length], start + length


def public_blob(public: bytes) -> bytes:
    """The SSH wire encoding of the public key, as in `ssh-ed25519 <base64 blob>`."""
    return _string(_KEY_TYPE) + _string(public)


def read_private_key(text: str) -> tuple[bytes, bytes] | None:
    """Returns (seed, public key) from an unencrypted OpenSSH Ed25519 private key, or None."""
    try:
        body = text.split(_PEM_BEGIN, 1)[1].split(_PEM_END, 1)[0]
        data = base64.b64decode("".join(body.split()))
        if not data.startswith(_MAGIC):
            return None
        offset = len(_MAGIC)
        cipher, offset = _read_string(data, offset)
        kdf, offset = _read_string(data, offset)
        _options, offset = _read_string(data, offset)
        (count,) = struct.unpack_from(">I", data, offset)
        offset += 4
        if cipher != b"none" or kdf != b"none" or count != 1:
            return None
        _public_blob, offset = _read_string(data, offset)
        private, offset = _read_string(data, offset)
        check1, check2 = struct.unpack_from(">II", private, 0)
        inner = 8
        key_type, inner = _read_string(private, inner)
        public, inner = _read_string(private, inner)
        secret, inner = _read_string(private, inner)
        if check1 != check2 or key_type != _KEY_TYPE or len(public) != 32 or len(secret) != 64:
            return None
        return secret[:32], public
    except (IndexError, ValueError, struct.error):
        return None


def write_private_key(seed: bytes, comment: str = "") -> str:
    """Encodes an Ed25519 key the way `ssh-keygen -t ed25519 -N ''` does."""
    public = public_key(seed)
    check = os.urandom(4)
    private = check + check + _string(_KEY_TYPE) + _string(public) + _string(seed + public) + _string(comment.encode())
    private += bytes(range(1, 1 + (-len(private) % 8)))
    data = _MAGIC + _string(b"none") + _string(b"none") + _string(b"") + struct.pack(">I", 1)
    data += _string(public_blob(public)) + _string(private)
    encoded = base64.b64encode(data).decode()
    lines = [encoded[index : index + 70] for index in range(0, len(encoded), 70)]
    return "\n".join([_PEM_BEGIN, *lines, _PEM_END]) + "\n"
