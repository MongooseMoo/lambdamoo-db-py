import struct

from lambdamoo_db.diff_types import Comparison, MISSING, UNSET, ReportOptions, canonical_json, encode_value
from lambdamoo_db.database import CLEAR, Anon, ObjNum, MooError


def test_typed_precision_and_sentinels():
    values = [1, True, ObjNum(1), MooError(1), CLEAR, None, MISSING, UNSET, Anon(-1)]
    assert len({canonical_json(encode_value(v)) for v in values}) == len(values)
    assert encode_value(-0.0) == {'type': 'float', 'bits': '8000000000000000'}
    nan = struct.unpack('>d', bytes.fromhex('7ff8000000000001'))[0]
    assert encode_value(nan)['bits'] == '7ff8000000000001'


def test_options_validate_and_normalize():
    assert ReportOptions(object_ids=(9, 2, 9)).object_ids == (2, 9)
    import pytest
    with pytest.raises(ValueError):
        ReportOptions(view='code', sections=('objects',))
    with pytest.raises(ValueError):
        ReportOptions(stop_after=0)


def test_status_exit_codes():
    assert [Comparison({}, [], {'status': s}).exit_code for s in ('equal', 'different', 'error', 'unknown')] == [0, 1, 2, 3]
