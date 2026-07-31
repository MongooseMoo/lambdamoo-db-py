"""Tests for database comparison module - TDD style."""

import pytest


class TestDiffPath:
    """Tests for DiffPath class."""

    def test_root_path_str(self):
        from lambdamoo_db.compare import DiffPath

        path = DiffPath.root()
        assert str(path) == "<root>"

    def test_root_path_segments_empty(self):
        from lambdamoo_db.compare import DiffPath

        path = DiffPath.root()
        assert path.segments == ()

    def test_object_path(self):
        from lambdamoo_db.compare import DiffPath

        path = DiffPath.object(123)
        assert str(path) == "#123"

    def test_child_string_segment(self):
        from lambdamoo_db.compare import DiffPath

        path = DiffPath.object(123).child("properties")
        assert str(path) == "#123.properties"

    def test_child_int_segment(self):
        from lambdamoo_db.compare import DiffPath

        path = DiffPath.object(123).child("verbs").child(0)
        assert str(path) == "#123.verbs[0]"

    def test_nested_path(self):
        from lambdamoo_db.compare import DiffPath

        path = DiffPath.object(123).child("properties").child(0).child("value")
        assert str(path) == "#123.properties[0].value"

    def test_deeply_nested_with_indices(self):
        from lambdamoo_db.compare import DiffPath

        path = (
            DiffPath.object(5)
            .child("properties")
            .child(2)
            .child("value")
            .child(0)
            .child(1)
        )
        assert str(path) == "#5.properties[2].value[0][1]"

    def test_path_immutability(self):
        from lambdamoo_db.compare import DiffPath

        path1 = DiffPath.root()
        path2 = path1.child("foo")
        # Original should be unchanged
        assert path1.segments == ()
        assert path2.segments == ("foo",)

    def test_path_is_frozen(self):
        from lambdamoo_db.compare import DiffPath

        path = DiffPath.root()
        with pytest.raises(Exception):  # attrs.FrozenInstanceError
            path.segments = ("bar",)

    def test_path_hashable(self):
        from lambdamoo_db.compare import DiffPath

        path1 = DiffPath.object(1).child("name")
        path2 = DiffPath.object(1).child("name")
        # Should be hashable and equal paths should hash the same
        assert hash(path1) == hash(path2)
        s = {path1, path2}
        assert len(s) == 1

    def test_waif_path(self):
        from lambdamoo_db.compare import DiffPath

        path = DiffPath.root().child("waifs").child(42).child("props").child(0)
        assert str(path) == "waifs[42].props[0]"


class TestDiffKind:
    """Tests for DiffKind enum."""

    def test_all_kinds_exist(self):
        from lambdamoo_db.compare import DiffKind

        assert DiffKind.VALUE_CHANGED.value == "changed"
        assert DiffKind.TYPE_MISMATCH.value == "type_mismatch"
        assert DiffKind.MISSING.value == "missing"
        assert DiffKind.EXTRA.value == "extra"
        assert DiffKind.LENGTH_MISMATCH.value == "length_mismatch"


class TestDiff:
    """Tests for Diff class."""

    def test_value_changed_str(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, Diff

        diff = Diff(
            path=DiffPath.object(1).child("name"),
            kind=DiffKind.VALUE_CHANGED,
            expected="foo",
            actual="bar",
        )
        s = str(diff)
        assert "#1.name" in s
        assert "foo" in s
        assert "bar" in s

    def test_type_mismatch_str(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, Diff

        diff = Diff(
            path=DiffPath.object(1).child("owner"),
            kind=DiffKind.TYPE_MISMATCH,
            expected=int,
            actual=str,
        )
        s = str(diff)
        assert "#1.owner" in s
        assert "int" in s
        assert "str" in s

    def test_missing_str(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, Diff

        diff = Diff(
            path=DiffPath.object(999),
            kind=DiffKind.MISSING,
            expected={"id": 999},
            actual=None,
        )
        s = str(diff)
        assert "#999" in s
        assert "MISSING" in s

    def test_extra_str(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, Diff

        diff = Diff(
            path=DiffPath.object(999),
            kind=DiffKind.EXTRA,
            expected=None,
            actual={"id": 999},
        )
        s = str(diff)
        assert "#999" in s
        assert "EXTRA" in s

    def test_length_mismatch_str(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, Diff

        diff = Diff(
            path=DiffPath.object(1).child("verbs"),
            kind=DiffKind.LENGTH_MISMATCH,
            expected=5,
            actual=3,
        )
        s = str(diff)
        assert "#1.verbs" in s
        assert "5" in s
        assert "3" in s

    def test_diff_is_frozen(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, Diff

        diff = Diff(
            path=DiffPath.root(),
            kind=DiffKind.VALUE_CHANGED,
            expected=1,
            actual=2,
        )
        with pytest.raises(Exception):
            diff.expected = 99


class TestCompareResult:
    """Tests for CompareResult class."""

    def test_empty_result_is_identical(self):
        from lambdamoo_db.compare import CompareResult

        result = CompareResult()
        assert result.identical
        assert len(result) == 0

    def test_result_with_diffs_not_identical(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, Diff, CompareResult

        result = CompareResult(
            diffs=[Diff(DiffPath.root(), DiffKind.VALUE_CHANGED, 1, 2)]
        )
        assert not result.identical
        assert len(result) == 1

    def test_bool_conversion(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, Diff, CompareResult

        empty = CompareResult()
        assert not bool(empty)  # Empty = falsy (no diffs)

        with_diffs = CompareResult(
            diffs=[Diff(DiffPath.root(), DiffKind.VALUE_CHANGED, 1, 2)]
        )
        assert bool(with_diffs)  # Has diffs = truthy

    def test_iteration(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, Diff, CompareResult

        diff1 = Diff(DiffPath.root(), DiffKind.VALUE_CHANGED, 1, 2)
        diff2 = Diff(DiffPath.root(), DiffKind.MISSING, 3, None)
        result = CompareResult(diffs=[diff1, diff2])

        collected = list(result)
        assert collected == [diff1, diff2]

    def test_filter_by_kind(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, Diff, CompareResult

        result = CompareResult(
            diffs=[
                Diff(DiffPath.object(1), DiffKind.VALUE_CHANGED, 1, 2),
                Diff(DiffPath.object(2), DiffKind.MISSING, 3, None),
                Diff(DiffPath.object(3), DiffKind.VALUE_CHANGED, 4, 5),
            ]
        )

        changed = result.filter_by_kind(DiffKind.VALUE_CHANGED)
        assert len(changed) == 2

        missing = result.filter_by_kind(DiffKind.MISSING)
        assert len(missing) == 1

    def test_filter_by_path_prefix(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, Diff, CompareResult

        result = CompareResult(
            diffs=[
                Diff(
                    DiffPath.object(1).child("name"), DiffKind.VALUE_CHANGED, "a", "b"
                ),
                Diff(DiffPath.object(1).child("owner"), DiffKind.VALUE_CHANGED, 1, 2),
                Diff(
                    DiffPath.object(2).child("name"), DiffKind.VALUE_CHANGED, "c", "d"
                ),
            ]
        )

        obj1_diffs = result.filter_by_path_prefix("#1")
        assert len(obj1_diffs) == 2

        obj2_diffs = result.filter_by_path_prefix("#2")
        assert len(obj2_diffs) == 1

    def test_summary(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, Diff, CompareResult

        # Empty
        empty = CompareResult()
        assert "identical" in empty.summary().lower()

        # With diffs
        result = CompareResult(
            diffs=[
                Diff(DiffPath.root(), DiffKind.VALUE_CHANGED, 1, 2),
                Diff(DiffPath.root(), DiffKind.MISSING, 3, None),
            ]
        )
        summary = result.summary()
        assert "2" in summary  # 2 differences
        assert "changed" in summary
        assert "missing" in summary

    def test_report_truncation(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, Diff, CompareResult

        diffs = [
            Diff(DiffPath.object(i), DiffKind.VALUE_CHANGED, i, i + 1)
            for i in range(100)
        ]
        result = CompareResult(diffs=diffs)

        report = result.report(max_diffs=10)
        assert "and 90 more" in report


class TestCompareValues:
    """Tests for compare_values function."""

    def test_equal_ints(self):
        from lambdamoo_db.compare import DiffPath, compare_values

        diffs = compare_values(DiffPath.root(), 42, 42)
        assert diffs == []

    def test_different_ints(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values

        diffs = compare_values(DiffPath.root(), 42, 43)
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.VALUE_CHANGED
        assert diffs[0].expected == 42
        assert diffs[0].actual == 43

    def test_equal_strings(self):
        from lambdamoo_db.compare import DiffPath, compare_values

        diffs = compare_values(DiffPath.root(), "hello", "hello")
        assert diffs == []

    def test_different_strings(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values

        diffs = compare_values(DiffPath.root(), "foo", "bar")
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.VALUE_CHANGED

    def test_equal_floats(self):
        from lambdamoo_db.compare import DiffPath, compare_values

        diffs = compare_values(DiffPath.root(), 3.14, 3.14)
        assert diffs == []

    def test_float_tolerance(self):
        from lambdamoo_db.compare import DiffPath, compare_values

        # Very close floats should be considered equal
        diffs = compare_values(DiffPath.root(), 1.0, 1.0 + 1e-12)
        assert diffs == []

    def test_both_none(self):
        from lambdamoo_db.compare import DiffPath, compare_values

        diffs = compare_values(DiffPath.root(), None, None)
        assert diffs == []

    def test_none_vs_value(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values

        diffs = compare_values(DiffPath.root(), None, 42)
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.VALUE_CHANGED

    def test_value_vs_none(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values

        diffs = compare_values(DiffPath.root(), 42, None)
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.VALUE_CHANGED

    def test_int_vs_objnum_type_mismatch(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values
        from lambdamoo_db.database import ObjNum

        diffs = compare_values(DiffPath.root(), 42, ObjNum(42))
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.TYPE_MISMATCH

    def test_equal_objnums(self):
        from lambdamoo_db.compare import DiffPath, compare_values
        from lambdamoo_db.database import ObjNum

        diffs = compare_values(DiffPath.root(), ObjNum(5), ObjNum(5))
        assert diffs == []

    def test_different_objnums(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values
        from lambdamoo_db.database import ObjNum

        diffs = compare_values(DiffPath.root(), ObjNum(5), ObjNum(6))
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.VALUE_CHANGED

    def test_equal_bools(self):
        from lambdamoo_db.compare import DiffPath, compare_values

        diffs = compare_values(DiffPath.root(), True, True)
        assert diffs == []

    def test_different_bools(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values

        diffs = compare_values(DiffPath.root(), True, False)
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.VALUE_CHANGED

    def test_clear_singleton(self):
        from lambdamoo_db.compare import DiffPath, compare_values
        from lambdamoo_db.database import CLEAR

        diffs = compare_values(DiffPath.root(), CLEAR, CLEAR)
        assert diffs == []

    def test_waif_reference_equal(self):
        from lambdamoo_db.compare import DiffPath, compare_values
        from lambdamoo_db.database import WaifReference

        diffs = compare_values(DiffPath.root(), WaifReference(5), WaifReference(5))
        assert diffs == []

    def test_waif_reference_hashable(self):
        from lambdamoo_db.database import WaifReference

        value = {WaifReference(5): "value"}
        assert value[WaifReference(5)] == "value"

    def test_waif_reference_different(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values
        from lambdamoo_db.database import WaifReference

        diffs = compare_values(DiffPath.root(), WaifReference(5), WaifReference(6))
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.VALUE_CHANGED


class TestCompareListValues:
    """Tests for list value comparison."""

    def test_equal_lists(self):
        from lambdamoo_db.compare import DiffPath, compare_values

        diffs = compare_values(DiffPath.root(), [1, 2, 3], [1, 2, 3])
        assert diffs == []

    def test_different_element(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values

        diffs = compare_values(DiffPath.root(), [1, 2, 3], [1, 99, 3])
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.VALUE_CHANGED
        assert "[1]" in str(diffs[0].path)

    def test_shorter_list(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values

        diffs = compare_values(DiffPath.root(), [1, 2, 3], [1, 2])
        # Should have length mismatch and missing element
        assert any(d.kind == DiffKind.LENGTH_MISMATCH for d in diffs)
        assert any(d.kind == DiffKind.MISSING for d in diffs)

    def test_longer_list(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values

        diffs = compare_values(DiffPath.root(), [1, 2], [1, 2, 3])
        # Should have length mismatch and extra element
        assert any(d.kind == DiffKind.LENGTH_MISMATCH for d in diffs)
        assert any(d.kind == DiffKind.EXTRA for d in diffs)

    def test_nested_list(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values

        expected = [1, [2, 3], 4]
        actual = [1, [2, 99], 4]
        diffs = compare_values(DiffPath.root(), expected, actual)
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.VALUE_CHANGED
        assert "[1][1]" in str(diffs[0].path)

    def test_empty_lists(self):
        from lambdamoo_db.compare import DiffPath, compare_values

        diffs = compare_values(DiffPath.root(), [], [])
        assert diffs == []


class TestCompareDictValues:
    """Tests for dict/map value comparison."""

    def test_equal_dicts(self):
        from lambdamoo_db.compare import DiffPath, compare_values

        diffs = compare_values(DiffPath.root(), {"a": 1, "b": 2}, {"a": 1, "b": 2})
        assert diffs == []

    def test_different_value(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values

        diffs = compare_values(DiffPath.root(), {"a": 1}, {"a": 2})
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.VALUE_CHANGED
        assert "a" in str(diffs[0].path)

    def test_missing_key(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values

        diffs = compare_values(DiffPath.root(), {"a": 1, "b": 2}, {"a": 1})
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.MISSING

    def test_extra_key(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values

        diffs = compare_values(DiffPath.root(), {"a": 1}, {"a": 1, "b": 2})
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.EXTRA

    def test_nested_dict(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_values

        expected = {"outer": {"inner": 1}}
        actual = {"outer": {"inner": 2}}
        diffs = compare_values(DiffPath.root(), expected, actual)
        assert len(diffs) == 1
        assert "inner" in str(diffs[0].path)

    def test_empty_dicts(self):
        from lambdamoo_db.compare import DiffPath, compare_values

        diffs = compare_values(DiffPath.root(), {}, {})
        assert diffs == []


class TestCompareProperties:
    """Tests for compare_properties function."""

    def test_identical_properties(self):
        from lambdamoo_db.compare import DiffPath, compare_properties
        from lambdamoo_db.database import Property
        from lambdamoo_db.enums import PropertyFlags

        props1 = [Property("name", "test", 1, PropertyFlags.READ)]
        props2 = [Property("name", "test", 1, PropertyFlags.READ)]
        diffs = compare_properties(DiffPath.object(1), props1, props2)
        assert diffs == []

    def test_different_value(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_properties
        from lambdamoo_db.database import Property
        from lambdamoo_db.enums import PropertyFlags

        props1 = [Property("name", "foo", 1, PropertyFlags.READ)]
        props2 = [Property("name", "bar", 1, PropertyFlags.READ)]
        diffs = compare_properties(DiffPath.object(1), props1, props2)
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.VALUE_CHANGED
        assert "name" in str(diffs[0].path)

    def test_different_owner(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_properties
        from lambdamoo_db.database import Property
        from lambdamoo_db.enums import PropertyFlags

        props1 = [Property("name", "test", 1, PropertyFlags.READ)]
        props2 = [Property("name", "test", 2, PropertyFlags.READ)]
        diffs = compare_properties(DiffPath.object(1), props1, props2)
        assert len(diffs) == 1
        assert "owner" in str(diffs[0].path)

    def test_different_perms(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_properties
        from lambdamoo_db.database import Property
        from lambdamoo_db.enums import PropertyFlags

        props1 = [Property("name", "test", 1, PropertyFlags.READ)]
        props2 = [Property("name", "test", 1, PropertyFlags.READ | PropertyFlags.WRITE)]
        diffs = compare_properties(DiffPath.object(1), props1, props2)
        assert len(diffs) == 1
        assert "perms" in str(diffs[0].path)

    def test_missing_property(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_properties
        from lambdamoo_db.database import Property
        from lambdamoo_db.enums import PropertyFlags

        props1 = [
            Property("name", "test", 1, PropertyFlags.READ),
            Property("extra", "val", 1, PropertyFlags.READ),
        ]
        props2 = [Property("name", "test", 1, PropertyFlags.READ)]
        diffs = compare_properties(DiffPath.object(1), props1, props2)
        assert any(d.kind == DiffKind.MISSING for d in diffs)

    def test_extra_property(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_properties
        from lambdamoo_db.database import Property
        from lambdamoo_db.enums import PropertyFlags

        props1 = [Property("name", "test", 1, PropertyFlags.READ)]
        props2 = [
            Property("name", "test", 1, PropertyFlags.READ),
            Property("extra", "val", 1, PropertyFlags.READ),
        ]
        diffs = compare_properties(DiffPath.object(1), props1, props2)
        assert any(d.kind == DiffKind.EXTRA for d in diffs)

    def test_empty_properties(self):
        from lambdamoo_db.compare import DiffPath, compare_properties

        diffs = compare_properties(DiffPath.object(1), [], [])
        assert diffs == []


class TestCompareVerbs:
    """Tests for compare_verbs function."""

    def test_identical_verbs(self):
        from lambdamoo_db.compare import DiffPath, compare_verbs
        from lambdamoo_db.database import Verb

        verbs1 = [Verb("test", 1, 7, -1, 1)]
        verbs1[0].code = ["return 1;"]
        verbs2 = [Verb("test", 1, 7, -1, 1)]
        verbs2[0].code = ["return 1;"]
        diffs = compare_verbs(DiffPath.object(1), verbs1, verbs2)
        assert diffs == []

    def test_different_code(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_verbs
        from lambdamoo_db.database import Verb

        verbs1 = [Verb("test", 1, 7, -1, 1)]
        verbs1[0].code = ["return 1;"]
        verbs2 = [Verb("test", 1, 7, -1, 1)]
        verbs2[0].code = ["return 2;"]
        diffs = compare_verbs(DiffPath.object(1), verbs1, verbs2)
        assert len(diffs) >= 1
        assert any("code" in str(d.path) for d in diffs)

    def test_different_perms(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_verbs
        from lambdamoo_db.database import Verb

        verbs1 = [Verb("test", 1, 7, -1, 1)]  # perms = 7 (rwx)
        verbs1[0].code = []
        verbs2 = [Verb("test", 1, 3, -1, 1)]  # perms = 3 (rw)
        verbs2[0].code = []
        diffs = compare_verbs(DiffPath.object(1), verbs1, verbs2)
        assert len(diffs) == 1
        assert "perms" in str(diffs[0].path)

    def test_different_preps(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_verbs
        from lambdamoo_db.database import Verb

        verbs1 = [Verb("test", 1, 7, -1, 1)]  # preps = -1 (none)
        verbs1[0].code = []
        verbs2 = [Verb("test", 1, 7, 0, 1)]  # preps = 0 (with/using)
        verbs2[0].code = []
        diffs = compare_verbs(DiffPath.object(1), verbs1, verbs2)
        assert len(diffs) == 1
        assert "preps" in str(diffs[0].path)

    def test_missing_verb(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_verbs
        from lambdamoo_db.database import Verb

        verbs1 = [Verb("test", 1, 7, -1, 1), Verb("extra", 1, 7, -1, 1)]
        verbs1[0].code = []
        verbs1[1].code = []
        verbs2 = [Verb("test", 1, 7, -1, 1)]
        verbs2[0].code = []
        diffs = compare_verbs(DiffPath.object(1), verbs1, verbs2)
        assert any(d.kind == DiffKind.MISSING for d in diffs)

    def test_extra_verb(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_verbs
        from lambdamoo_db.database import Verb

        verbs1 = [Verb("test", 1, 7, -1, 1)]
        verbs1[0].code = []
        verbs2 = [Verb("test", 1, 7, -1, 1), Verb("extra", 1, 7, -1, 1)]
        verbs2[0].code = []
        verbs2[1].code = []
        diffs = compare_verbs(DiffPath.object(1), verbs1, verbs2)
        assert any(d.kind == DiffKind.EXTRA for d in diffs)

    def test_none_vs_empty_code(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_verbs
        from lambdamoo_db.database import Verb

        verbs1 = [Verb("test", 1, 7, -1, 1)]
        verbs1[0].code = None  # No program
        verbs2 = [Verb("test", 1, 7, -1, 1)]
        verbs2[0].code = []  # Empty program
        diffs = compare_verbs(DiffPath.object(1), verbs1, verbs2)
        # None vs [] should be considered different
        assert len(diffs) >= 1


class TestCompareObjects:
    """Tests for compare_objects function."""

    def test_identical_objects(self):
        from lambdamoo_db.compare import DiffPath, compare_objects
        from lambdamoo_db.database import MooObject, ObjNum
        from lambdamoo_db.enums import ObjectFlags

        obj1 = MooObject(1, "Test", ObjectFlags.READ, 2, ObjNum(-1), [0])
        obj2 = MooObject(1, "Test", ObjectFlags.READ, 2, ObjNum(-1), [0])
        diffs = compare_objects(DiffPath.object(1), obj1, obj2)
        assert diffs == []

    def test_different_name(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_objects
        from lambdamoo_db.database import MooObject, ObjNum
        from lambdamoo_db.enums import ObjectFlags

        obj1 = MooObject(1, "Foo", ObjectFlags.READ, 2, ObjNum(-1), [0])
        obj2 = MooObject(1, "Bar", ObjectFlags.READ, 2, ObjNum(-1), [0])
        diffs = compare_objects(DiffPath.object(1), obj1, obj2)
        assert len(diffs) == 1
        assert "name" in str(diffs[0].path)

    def test_different_flags(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_objects
        from lambdamoo_db.database import MooObject, ObjNum
        from lambdamoo_db.enums import ObjectFlags

        obj1 = MooObject(1, "Test", ObjectFlags.READ, 2, ObjNum(-1), [0])
        obj2 = MooObject(
            1, "Test", ObjectFlags.READ | ObjectFlags.WRITE, 2, ObjNum(-1), [0]
        )
        diffs = compare_objects(DiffPath.object(1), obj1, obj2)
        assert len(diffs) == 1
        assert "flags" in str(diffs[0].path)

    def test_different_owner(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_objects
        from lambdamoo_db.database import MooObject, ObjNum
        from lambdamoo_db.enums import ObjectFlags

        obj1 = MooObject(1, "Test", ObjectFlags.READ, 2, ObjNum(-1), [0])
        obj2 = MooObject(1, "Test", ObjectFlags.READ, 3, ObjNum(-1), [0])
        diffs = compare_objects(DiffPath.object(1), obj1, obj2)
        assert len(diffs) == 1
        assert "owner" in str(diffs[0].path)

    def test_different_location(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_objects
        from lambdamoo_db.database import MooObject, ObjNum
        from lambdamoo_db.enums import ObjectFlags

        obj1 = MooObject(1, "Test", ObjectFlags.READ, 2, ObjNum(100), [0])
        obj2 = MooObject(1, "Test", ObjectFlags.READ, 2, ObjNum(200), [0])
        diffs = compare_objects(DiffPath.object(1), obj1, obj2)
        assert len(diffs) == 1
        assert "location" in str(diffs[0].path)

    def test_different_parents(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_objects
        from lambdamoo_db.database import MooObject, ObjNum
        from lambdamoo_db.enums import ObjectFlags

        obj1 = MooObject(1, "Test", ObjectFlags.READ, 2, ObjNum(-1), [0])
        obj2 = MooObject(1, "Test", ObjectFlags.READ, 2, ObjNum(-1), [0, 5])
        diffs = compare_objects(DiffPath.object(1), obj1, obj2)
        assert any("parents" in str(d.path) for d in diffs)


class TestCompareWaif:
    """Tests for compare_waif function (individual waifs)."""

    def test_identical_waifs(self):
        from lambdamoo_db.compare import DiffPath, compare_waif
        from lambdamoo_db.database import Waif

        waif1 = Waif(waif_class=100, owner=1, props=[(0, "value1"), (2, 42)])
        waif1.propdefs_length = 5
        waif2 = Waif(waif_class=100, owner=1, props=[(0, "value1"), (2, 42)])
        waif2.propdefs_length = 5

        diffs = compare_waif(DiffPath.root().child("waifs").child(0), waif1, waif2)
        assert diffs == []

    def test_different_waif_class(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_waif
        from lambdamoo_db.database import Waif

        waif1 = Waif(waif_class=100, owner=1, props=[])
        waif2 = Waif(waif_class=200, owner=1, props=[])

        diffs = compare_waif(DiffPath.root().child("waifs").child(0), waif1, waif2)
        assert len(diffs) == 1
        assert diffs[0].kind == DiffKind.VALUE_CHANGED
        assert "waif_class" in str(diffs[0].path)

    def test_different_owner(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_waif
        from lambdamoo_db.database import Waif

        waif1 = Waif(waif_class=100, owner=1, props=[])
        waif2 = Waif(waif_class=100, owner=2, props=[])

        diffs = compare_waif(DiffPath.root().child("waifs").child(0), waif1, waif2)
        assert len(diffs) == 1
        assert "owner" in str(diffs[0].path)

    def test_different_propdefs_length(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_waif
        from lambdamoo_db.database import Waif

        waif1 = Waif(waif_class=100, owner=1, props=[])
        waif1.propdefs_length = 5
        waif2 = Waif(waif_class=100, owner=1, props=[])
        waif2.propdefs_length = 10

        diffs = compare_waif(DiffPath.root().child("waifs").child(0), waif1, waif2)
        assert len(diffs) == 1
        assert "propdefs_length" in str(diffs[0].path)

    def test_different_props_values(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_waif
        from lambdamoo_db.database import Waif

        waif1 = Waif(waif_class=100, owner=1, props=[(0, "foo"), (1, 42)])
        waif2 = Waif(waif_class=100, owner=1, props=[(0, "bar"), (1, 42)])

        diffs = compare_waif(DiffPath.root().child("waifs").child(0), waif1, waif2)
        assert len(diffs) >= 1
        assert any("props" in str(d.path) for d in diffs)

    def test_different_props_length(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_waif
        from lambdamoo_db.database import Waif

        waif1 = Waif(waif_class=100, owner=1, props=[(0, "foo")])
        waif2 = Waif(waif_class=100, owner=1, props=[(0, "foo"), (1, "bar")])

        diffs = compare_waif(DiffPath.root().child("waifs").child(0), waif1, waif2)
        assert any(d.kind == DiffKind.LENGTH_MISMATCH for d in diffs)


class TestCompareWaifs:
    """Tests for compare_waifs function (dict of waifs)."""

    def test_identical_waifs_dict(self):
        from lambdamoo_db.compare import DiffPath, compare_waifs
        from lambdamoo_db.database import Waif

        waifs1 = {
            0: Waif(waif_class=100, owner=1, props=[(0, "a")]),
            1: Waif(waif_class=200, owner=2, props=[(0, "b")]),
        }
        waifs2 = {
            0: Waif(waif_class=100, owner=1, props=[(0, "a")]),
            1: Waif(waif_class=200, owner=2, props=[(0, "b")]),
        }

        diffs = compare_waifs(DiffPath.root(), waifs1, waifs2)
        assert diffs == []

    def test_missing_waif(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_waifs
        from lambdamoo_db.database import Waif

        waifs1 = {
            0: Waif(waif_class=100, owner=1, props=[]),
            1: Waif(waif_class=200, owner=1, props=[]),
        }
        waifs2 = {
            0: Waif(waif_class=100, owner=1, props=[]),
        }

        diffs = compare_waifs(DiffPath.root(), waifs1, waifs2)
        assert any(d.kind == DiffKind.MISSING for d in diffs)

    def test_extra_waif(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_waifs
        from lambdamoo_db.database import Waif

        waifs1 = {
            0: Waif(waif_class=100, owner=1, props=[]),
        }
        waifs2 = {
            0: Waif(waif_class=100, owner=1, props=[]),
            1: Waif(waif_class=200, owner=1, props=[]),
        }

        diffs = compare_waifs(DiffPath.root(), waifs1, waifs2)
        assert any(d.kind == DiffKind.EXTRA for d in diffs)

    def test_different_waif_content(self):
        from lambdamoo_db.compare import DiffPath, DiffKind, compare_waifs
        from lambdamoo_db.database import Waif

        waifs1 = {0: Waif(waif_class=100, owner=1, props=[])}
        waifs2 = {0: Waif(waif_class=999, owner=1, props=[])}

        diffs = compare_waifs(DiffPath.root(), waifs1, waifs2)
        assert len(diffs) >= 1
        assert any("waif_class" in str(d.path) for d in diffs)

    def test_empty_waifs(self):
        from lambdamoo_db.compare import DiffPath, compare_waifs

        diffs = compare_waifs(DiffPath.root(), {}, {})
        assert diffs == []


class TestCompareDatabases:
    """Tests for compare_databases function."""

    def test_identical_empty_databases(self):
        from lambdamoo_db.compare import compare_databases
        from lambdamoo_db.database import MooDatabase

        db1 = MooDatabase()
        db1.version = 17
        db1.versionstring = "** LambdaMOO Database, Format Version 17 **"
        db2 = MooDatabase()
        db2.version = 17
        db2.versionstring = "** LambdaMOO Database, Format Version 17 **"

        result = compare_databases(db1, db2)
        assert result.identical

    def test_different_version(self):
        from lambdamoo_db.compare import compare_databases, DiffKind
        from lambdamoo_db.database import MooDatabase

        db1 = MooDatabase()
        db1.version = 17
        db1.versionstring = "** LambdaMOO Database, Format Version 17 **"
        db2 = MooDatabase()
        db2.version = 16
        db2.versionstring = "** LambdaMOO Database, Format Version 16 **"

        result = compare_databases(db1, db2)
        assert not result.identical
        assert any("version" in str(d.path) for d in result.diffs)

    def test_databases_with_objects(self):
        from lambdamoo_db.compare import compare_databases
        from lambdamoo_db.database import MooDatabase, MooObject, ObjNum
        from lambdamoo_db.enums import ObjectFlags

        db1 = MooDatabase()
        db1.version = 17
        db1.versionstring = "test"
        db1.objects = {0: MooObject(0, "Root", ObjectFlags.READ, 0, ObjNum(-1), [])}

        db2 = MooDatabase()
        db2.version = 17
        db2.versionstring = "test"
        db2.objects = {0: MooObject(0, "Root", ObjectFlags.READ, 0, ObjNum(-1), [])}

        result = compare_databases(db1, db2)
        assert result.identical

    def test_different_object_count(self):
        from lambdamoo_db.compare import compare_databases, DiffKind
        from lambdamoo_db.database import MooDatabase, MooObject, ObjNum
        from lambdamoo_db.enums import ObjectFlags

        db1 = MooDatabase()
        db1.version = 17
        db1.versionstring = "test"
        db1.objects = {
            0: MooObject(0, "Root", ObjectFlags.READ, 0, ObjNum(-1), []),
            1: MooObject(1, "Extra", ObjectFlags.READ, 0, ObjNum(-1), []),
        }

        db2 = MooDatabase()
        db2.version = 17
        db2.versionstring = "test"
        db2.objects = {0: MooObject(0, "Root", ObjectFlags.READ, 0, ObjNum(-1), [])}

        result = compare_databases(db1, db2)
        assert not result.identical
        assert any(d.kind == DiffKind.MISSING for d in result.diffs)

    def test_anon_objects(self):
        """Test that anon objects are properly compared."""
        from lambdamoo_db.compare import compare_databases
        from lambdamoo_db.database import MooDatabase, MooObject, ObjNum
        from lambdamoo_db.enums import ObjectFlags

        # Create anon objects (anon=True)
        anon1 = MooObject(1000, "Anon", ObjectFlags.READ, 0, ObjNum(-1), [], anon=True)
        anon2 = MooObject(1000, "Anon", ObjectFlags.READ, 0, ObjNum(-1), [], anon=True)

        db1 = MooDatabase()
        db1.version = 17
        db1.versionstring = "test"
        db1.objects = {1000: anon1}

        db2 = MooDatabase()
        db2.version = 17
        db2.versionstring = "test"
        db2.objects = {1000: anon2}

        result = compare_databases(db1, db2)
        assert result.identical

    def test_anon_vs_regular_object(self):
        """Test that anon flag difference is detected."""
        from lambdamoo_db.compare import compare_databases, DiffKind
        from lambdamoo_db.database import MooDatabase, MooObject, ObjNum
        from lambdamoo_db.enums import ObjectFlags

        # One anon, one regular
        obj1 = MooObject(1, "Test", ObjectFlags.READ, 0, ObjNum(-1), [], anon=True)
        obj2 = MooObject(1, "Test", ObjectFlags.READ, 0, ObjNum(-1), [], anon=False)

        db1 = MooDatabase()
        db1.version = 17
        db1.versionstring = "test"
        db1.objects = {1: obj1}

        db2 = MooDatabase()
        db2.version = 17
        db2.versionstring = "test"
        db2.objects = {1: obj2}

        result = compare_databases(db1, db2)
        assert not result.identical
        assert any("anon" in str(d.path) for d in result.diffs)

    def test_pending_anon_ids(self):
        """Test that pending_anon_ids are compared."""
        from lambdamoo_db.compare import compare_databases, DiffKind
        from lambdamoo_db.database import MooDatabase

        db1 = MooDatabase()
        db1.version = 17
        db1.versionstring = "test"
        db1.pending_anon_ids = [100, 101, 102]

        db2 = MooDatabase()
        db2.version = 17
        db2.versionstring = "test"
        db2.pending_anon_ids = [100, 101]

        result = compare_databases(db1, db2)
        assert not result.identical
        assert any("pending_anon_ids" in str(d.path) for d in result.diffs)

    def test_databases_with_waifs(self):
        from lambdamoo_db.compare import compare_databases
        from lambdamoo_db.database import MooDatabase, Waif

        db1 = MooDatabase()
        db1.version = 17
        db1.versionstring = "test"
        db1.waifs = {0: Waif(waif_class=100, owner=1, props=[])}

        db2 = MooDatabase()
        db2.version = 17
        db2.versionstring = "test"
        db2.waifs = {0: Waif(waif_class=100, owner=1, props=[])}

        result = compare_databases(db1, db2)
        assert result.identical

    def test_different_waifs(self):
        from lambdamoo_db.compare import compare_databases, DiffKind
        from lambdamoo_db.database import MooDatabase, Waif

        db1 = MooDatabase()
        db1.version = 17
        db1.versionstring = "test"
        db1.waifs = {0: Waif(waif_class=100, owner=1, props=[])}

        db2 = MooDatabase()
        db2.version = 17
        db2.versionstring = "test"
        db2.waifs = {0: Waif(waif_class=999, owner=1, props=[])}

        result = compare_databases(db1, db2)
        assert not result.identical

    def test_different_players(self):
        from lambdamoo_db.compare import compare_databases, DiffKind
        from lambdamoo_db.database import MooDatabase

        db1 = MooDatabase()
        db1.version = 17
        db1.versionstring = "test"
        db1.players = [1, 2, 3]

        db2 = MooDatabase()
        db2.version = 17
        db2.versionstring = "test"
        db2.players = [1, 2]

        result = compare_databases(db1, db2)
        assert not result.identical
        assert any("players" in str(d.path) for d in result.diffs)

    def test_different_recycled_objects(self):
        from lambdamoo_db.compare import compare_databases, DiffKind
        from lambdamoo_db.database import MooDatabase

        db1 = MooDatabase()
        db1.version = 17
        db1.versionstring = "test"
        db1.recycled_objects = {5, 10, 15}

        db2 = MooDatabase()
        db2.version = 17
        db2.versionstring = "test"
        db2.recycled_objects = {5, 10}

        result = compare_databases(db1, db2)
        assert not result.identical
        assert any("recycled_objects" in str(d.path) for d in result.diffs)

    def test_max_diffs_limit(self):
        """Test that max_diffs parameter limits results."""
        from lambdamoo_db.compare import compare_databases
        from lambdamoo_db.database import MooDatabase, MooObject, ObjNum
        from lambdamoo_db.enums import ObjectFlags

        # Create many different objects
        db1 = MooDatabase()
        db1.version = 17
        db1.versionstring = "test"
        db1.objects = {
            i: MooObject(i, f"Obj{i}", ObjectFlags.READ, 0, ObjNum(-1), [])
            for i in range(100)
        }

        db2 = MooDatabase()
        db2.version = 17
        db2.versionstring = "test"
        db2.objects = {
            i: MooObject(i, f"Different{i}", ObjectFlags.READ, 0, ObjNum(-1), [])
            for i in range(100)
        }

        result = compare_databases(db1, db2, max_diffs=10)
        assert len(result.diffs) == 10

    def test_ignore_fields(self):
        """Test that ignore_fields parameter works."""
        from lambdamoo_db.compare import compare_databases
        from lambdamoo_db.database import MooDatabase

        db1 = MooDatabase()
        db1.version = 17
        db1.versionstring = "test"
        db1.players = [1, 2, 3]

        db2 = MooDatabase()
        db2.version = 17
        db2.versionstring = "test"
        db2.players = [1, 2]

        # Without ignore - should have diffs
        result = compare_databases(db1, db2)
        assert not result.identical

        # With ignore - should be identical
        result = compare_databases(db1, db2, ignore_fields={"players"})
        assert result.identical
