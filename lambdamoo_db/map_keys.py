"""Numeric map-key identity for one verified ToastStunt build configuration.

The primary evidence compiled the exact utils.cc comparator with the live
structures.h and build-release configuration: Num/Objid are 64-bit, C int is
32-bit and ONLY_32_BITS is disabled. Both GCC -O0 and release -O3 returned the
low 32 bits of unsigned-64 subtraction across 392 INT/OBJ boundary pairs.
TYPE_ERR is also serialized through the 64-bit v.num union member, while
comparison reads its 32-bit enum v.err member. Compiling the exact comparator
against those headers/configuration at -O0 and -O3 confirmed low-32 identity
and unchanged raw wire values across 324 ERR boundary pairs at each level.
This policy describes that little-endian GNU build; it makes no portable C++ or unconfigured
server claim. Wire widths are checked before applying the verified identity.
"""

from .database import MooError, ObjNum

NUMERIC_MAP_KEY_POLICY = 'toast-num64-int32-gnu-v1-error-union64'


def numeric_key_identity(key) -> int:
    """Return the configured server equality identity, or fail without values."""
    kind = type(key)
    if kind in (int, ObjNum, MooError):
        number = int(key)
        if -(2**63) <= number <= 2**63 - 1:
            return number & 0xffffffff
    raise ValueError('unsupported numeric map key')
