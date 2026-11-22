from pathlib import Path

from click.testing import CliRunner

from lambdamoo_db.cli import moodb
from lambdamoo_db.database import CLEAR, MooDatabase, MooObject, ObjNum, Property, Waif, WaifReference
from lambdamoo_db.inspection import material_species_names, material_species_refs

TOASTCORE = Path(__file__).parent.parent / "toastcore.db"


def material_db() -> MooDatabase:
    """#1 a material engine, #2 a thing declaring a material, #3 a form with form-material uses,
    #4 a waif class holding a record, #5 a holder of that waif, #6 inheriting #2's declaration clear."""
    db = MooDatabase()

    def add(num, parents, own, values):
        o = MooObject(num, f"obj{num}", 0, 0, -1, [ObjNum(p) for p in parents])
        names = own + [None] * (len(values) - len(own))
        o.properties = [Property(n, v, 0, 5) for n, v in zip(names, values)]
        o.propdefs_count = len(own)
        db.objects[num] = o

    add(0, [], ["material_engine"], [ObjNum(1)])
    add(1, [], ["profiles", "species_aliases", "family_species"],
        [{"steel": {}, "H2O": {}, "humus": {}}, {"water": "H2O", "organic_matter": "humus"}, {"wood": ["oak"], "metal": ["steel"]}])
    add(2, [], ["capabilities"], [{"material": {"components": [
        {"species": "steel", "fraction": 0.5}, {"species": "Water", "fraction": 0.3}, {"species": "KCl", "fraction": 0.2}]}}])
    # .parts rows are {definition, count, settings}; settings["material"] is form-material.
    add(3, [], ["parts"], [[[ObjNum(2), 1, {"role": "frame", "material": "wood"}],
                            [ObjNum(2), 1, {"role": "seat", "material": "unobtainium"}],
                            [ObjNum(2), 1, {"role": "leg"}]]])
    add(4, [], [":record"], [0])
    add(5, [], ["liquids"], [[WaifReference(0)]])
    add(6, [2], [], [CLEAR])
    db.waifs[0] = Waif(4, 2, [(0, {"components": [{"species": "organic_matter", "fraction": 1.0}]})], 1)
    return db


def test_material_species_refs_finds_components_and_form_materials_with_paths():
    refs = sorted((r.obj.id, r.where, r.species, r.kind) for r in material_species_refs(material_db()))
    assert refs == [
        (2, '.capabilities["material"]["components"][1]["species"]', "steel", "component"),
        (2, '.capabilities["material"]["components"][2]["species"]', "Water", "component"),
        (2, '.capabilities["material"]["components"][3]["species"]', "KCl", "component"),
        (3, '.parts[1][3]["material"]', "wood", "form-material"),
        (3, '.parts[2][3]["material"]', "unobtainium", "form-material"),
        (5, '.liquids[1]<waif #4>.record["components"][1]["species"]', "organic_matter", "component"),
    ]


def test_material_species_names_resolve_aliases_case_and_families():
    db = material_db()
    rows = {(r.species, r.kind): (r.canonical, r.known, len(r.refs)) for r in material_species_names(db, db.objects[1])}
    assert rows == {
        ("KCl", "component"): ("KCl", False, 1),
        ("organic_matter", "component"): ("humus", True, 1),
        ("steel", "component"): ("steel", True, 1),
        ("Water", "component"): ("H2O", True, 1),  # alias keys fold case, as MOO MAP keys do
        ("unobtainium", "form-material"): ("unobtainium", False, 1),
        ("wood", "form-material"): ("wood", True, 1),  # a form-material may name a family
    }


def run(*args):
    return CliRunner().invoke(moodb, ["--db", str(TOASTCORE), "--no-cache", *args])


def test_cli_material_species_needs_a_material_engine():
    r = run("material-species", "--unknown")
    assert r.exit_code == 1 and "#0 has no $material_engine" in r.output
