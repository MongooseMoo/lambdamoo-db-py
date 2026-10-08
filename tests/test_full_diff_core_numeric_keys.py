from copy import deepcopy

import pytest

from lambdamoo_db.compare import compare_snapshots
from lambdamoo_db.database import MooDatabase, MooObject, MooError, ObjNum, Property
from lambdamoo_db.diff_types import ReportOptions
from lambdamoo_db.map_keys import NUMERIC_MAP_KEY_POLICY


def database(value):
    root = MooObject(1, 'synthetic', 0, 1, -1)
    root.properties = [Property('value', value, 1, 0)]
    root.propdefs_count = 1
    result = MooDatabase(objects={1: root})
    result.version = 17
    result.versionstring = 'synthetic'
    result.total_objects = 1
    return result


@pytest.mark.parametrize('cls', [int, ObjNum, MooError])
def test_configured_numeric_key_equivalence_world_and_exact_checkpoint(cls):
    old = database({cls(1): 'same'})
    new = database({cls(2**32 + 1): 'same'})
    result = compare_snapshots(old, new)
    assert result.exit_code == 0
    assert result.header['numeric_map_key_policy'] == NUMERIC_MAP_KEY_POLICY
    assert NUMERIC_MAP_KEY_POLICY in result.header['policy_version']
    assert compare_snapshots(old, new, ReportOptions(view='checkpoint')).exit_code == 1


@pytest.mark.parametrize('cls', [int, ObjNum, MooError])
def test_same_side_configured_numeric_key_collisions_fail(cls):
    source = database({cls(1): 'a', cls(2**32 + 1): 'b'})
    assert compare_snapshots(source, source).exit_code == 2


@pytest.mark.parametrize('key', [2**63, -(2**63) - 1, ObjNum(2**63), MooError(2**63), MooError(-(2**63) - 1)])
def test_numeric_keys_outside_configured_wire_width_fail(key):
    source = database({key: 'value'})
    assert compare_snapshots(source, source).exit_code == 2


@pytest.mark.parametrize('key', [-(2**63), 2**63 - 1, ObjNum(-(2**63)), ObjNum(2**63 - 1), MooError(-(2**63)), MooError(2**63 - 1)])
def test_configured_numeric_key_wire_bounds_pass(key):
    source = database({key: 'value'})
    assert compare_snapshots(source, source).exit_code == 0


def test_scalar_values_retain_arbitrary_precision():
    source = database(2**80)
    assert compare_snapshots(source, source).exit_code == 0
    changed = deepcopy(source)
    changed.objects[1].properties[0].value += 1
    assert compare_snapshots(source, changed).exit_code == 1


def test_raw_record_table_ids_are_not_server_map_keys():
    source = database(1)
    additional = MooObject(2**32 + 1, 'distinct raw record', 0, 1, -1)
    source.objects[additional.id] = additional
    source.total_objects = 2
    assert compare_snapshots(source, source).exit_code == 0
