"""The Mirasim frame check: a host bundle read for the literals the adapter depends on."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts import check_mirasim_frames as check

ADAPTER = Path(__file__).resolve().parents[1] / "raven" / "acp" / "mirasim"


def bundle(*, drop: str = "", split: bool = False) -> str:
    """A stand-in bundle that names everything the adapter uses, minified the way Mirasim's is."""
    cases = "".join(f"case'{name}':break;" for name in check.SENT if name != drop)
    types = "".join(f"send({{'type':'{name}'}});" for name in check.RECEIVED if name != drop)
    fields = "".join(f"x['{name}'];" for name in check.FIELDS if name != drop)
    text = "/[^']/.test(s);" + cases + types + fields
    if split:
        text = text.replace("'appendText'", "'appendTe'+'xt'").replace("case'listClis'", "case'list'+'Clis'")
    return text


def test_a_bundle_with_everything_misses_nothing() -> None:
    assert check.missing(bundle()) == {}
    assert check.missing(bundle(split=True)) == {}


@pytest.mark.parametrize(
    ("name", "kind"),
    [("steer", "sent frames (no case label)"), ("accepted", "read frames (no type literal)"), ("fromSeq", "fields")],
)
def test_what_is_missing_is_named_by_kind(name: str, kind: str) -> None:
    assert check.missing(bundle(drop=name)) == {kind: [name]}


def test_a_quote_inside_a_regular_expression_hides_no_field() -> None:
    assert check.quoted("a=/[^']/;b={'fromSeq':1}", "fromSeq")
    assert not check.quoted("'fromSeqs'", "fromSeq")


def test_changes_list_the_frame_names_that_appeared_or_vanished_and_count_the_rest() -> None:
    old = "case'prompt':case'gone':send({'type':'accepted'});"
    new = (
        "case'prompt':case'fresh':send({'type':'accepted'});send({'type':'newFrame'});"
        "s={'type':'ChatMediaKind'};t={'type':'Row[\\x27kind\\x27]'};"
    )
    assert check.changes(old, new) == {
        "case labels added": ["fresh"],
        "case labels removed": ["gone"],
        "type literals added": ["newFrame"],
        "type literals removed": [],
        "other literals changed": ["2"],
    }


def test_a_name_renamed_in_one_of_the_places_that_say_it_shows_as_fewer() -> None:
    old = bundle() + "case'stop':break;send({'type':'session'});"
    new = bundle()
    assert check.missing(new) == {}
    assert check.fewer(old, new) == {"case 'stop'": (2, 1), "type 'session'": (2, 1)}
    assert check.fewer(new, old) == {}


def test_the_entry_reports_and_exits_by_what_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    good, bad = tmp_path / "good.cjs", tmp_path / "bad.cjs"
    good.write_text(bundle())
    bad.write_text(bundle(drop="interact"))
    assert check.main([str(good)]) == 0
    assert "all 13 sent frames" in capsys.readouterr().out
    assert check.main([str(bad), "--against", str(good)]) == 1
    out = capsys.readouterr().out
    assert "case labels removed: interact" in out and "MISSING sent frames (no case label): interact" in out
    assert "FEWER case 'interact': 1 before, 0 now" in out
    renamed = tmp_path / "renamed.cjs"
    renamed.write_text(bundle())
    twice = tmp_path / "twice.cjs"
    twice.write_text(bundle() + "case'stop':break;")
    assert check.main([str(renamed), "--against", str(twice)]) == 1
    assert "FEWER case 'stop': 2 before, 1 now" in capsys.readouterr().out
    assert check.main([str(tmp_path / "absent.cjs")]) == 2
    assert "pass the host's server.cjs" not in capsys.readouterr().err
    monkeypatch.setattr(check, "DEFAULT_BUNDLE", tmp_path / "no-host" / "server.cjs")
    assert check.main([]) == 2
    assert "pass the host's server.cjs" in capsys.readouterr().err
    monkeypatch.setattr(check, "DEFAULT_BUNDLE", good)
    assert check.main([]) == 0


NOT_FRAMES = {
    "text",
    "image",
    "resource",
    "resource_link",
    "content",
    "select",
    "array",
    "object",
    "string",
}
"""``"type"`` values the adapter spells that are not host frames: ACP content blocks, the type of a
question a turn asks, and the JSON Schema types of the form that asks it."""


def _adapter() -> list[ast.Module]:
    return [ast.parse(path.read_text(encoding="utf-8")) for path in sorted(ADAPTER.glob("*.py"))]


def _strings(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return {element.value for element in node.elts if isinstance(element, ast.Constant)}
    return set()


def _sent(trees: list[ast.Module]) -> tuple[set[str], set[str]]:
    """The frame types written as dict literals, and every key those frames are given."""
    frames: set[str] = set()
    keys: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                literal = {
                    k.value: v for k, v in zip(node.keys, node.values, strict=True) if isinstance(k, ast.Constant)
                }
                kind = _strings(literal.get("type", ast.Constant(None)))
                if kind - NOT_FRAMES:
                    frames |= kind
                    keys |= {key for key in literal if isinstance(key, str) and key != "type"}
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "frame"
                    ):
                        keys |= _strings(target.slice)
    return frames, keys


def _read(trees: list[ast.Module]) -> set[str]:
    """The frame types a frame's ``type`` is compared with, however the comparison is spelled."""
    read: set[str] = set()
    for tree in trees:
        typed = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and _is_type_of(node.value)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and (
                _is_type_of(node.left) or (isinstance(node.left, ast.Name) and node.left.id in typed)
            ):
                for comparator in node.comparators:
                    read |= _strings(comparator)
    return read - NOT_FRAMES


def _is_type_of(node: ast.AST) -> bool:
    if isinstance(node, ast.Subscript):
        return _strings(node.slice) == {"type"}
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and _strings(node.args[0] if node.args else ast.Constant(None)) == {"type"}
    )


def test_the_scan_finds_a_frame_however_its_dict_or_its_comparison_is_spelled() -> None:
    tree = ast.parse(
        'a = {"sessionKey": key, "type": "stopTask"}\n'
        'if frame["type"] == "memoryGuardStopped": pass\n'
        'kind = f.get("type")\n'
        'if kind in ("x", "y"): pass\n'
    )
    assert _sent([tree]) == ({"stopTask"}, {"sessionKey"})
    assert _read([tree]) == {"memoryGuardStopped", "x", "y"}


def test_every_frame_the_adapter_sends_is_checked_with_every_field_it_gives_them() -> None:
    frames, keys = _sent(_adapter())
    assert {"getHistory", "prompt", "steer", "setStreamCadence"} <= frames
    assert frames <= set(check.SENT), frames - set(check.SENT)
    assert {"coalesceMs", "statusMs", "sessionKey", "route"} <= keys
    assert keys <= set(check.FIELDS), keys - set(check.FIELDS)


def test_every_frame_the_adapter_reads_is_checked() -> None:
    read = _read(_adapter())
    assert {"accepted", "outbox", "snapshot", "session", "clis"} <= read
    assert read <= set(check.RECEIVED) | set(check.TOLERATED), read - set(check.RECEIVED)
