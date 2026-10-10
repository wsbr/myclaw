"""Lossless bounded text pages and deterministic Workspace write conflicts."""

import codecs
import hashlib
import io

import pytest

from app.core.tool_registry.base import RuntimeContext
from app.core.tool_registry.builtin.read_file import ReadFileTool, _read_text_page
from app.core.tool_registry.builtin.write_file import WriteFileTool


pytestmark = pytest.mark.unit


@pytest.fixture
def file_tools(tmp_path, monkeypatch):
    async def resolve_read(db, *, context, value):
        return tmp_path / value, value

    async def resolve_write(db, *, relative_path, **kwargs):
        return tmp_path, tmp_path / relative_path, relative_path

    monkeypatch.setattr(
        "app.core.tool_registry.builtin.read_file.resolve_read_path", resolve_read,
    )
    monkeypatch.setattr(
        "app.core.tool_registry.builtin.write_file.resolve_workspace_path", resolve_write,
    )
    context = RuntimeContext(
        tenant_id="tenant", session_id="session", runner_id="runner",
        user_id="user", agent_id="agent",
    )
    return tmp_path, context


def encoded(text, encoding):
    if encoding == "utf-16-le":
        return codecs.BOM_UTF16_LE + text.encode(encoding)
    if encoding == "utf-16-be":
        return codecs.BOM_UTF16_BE + text.encode(encoding)
    return text.encode(encoding)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 7, 100, 65536])
async def test_pages_reassemble_unicode_without_loss(file_tools, encoding, limit):
    root, context = file_tools
    # The UAT failure cut a multibyte character at byte 100. Also exercise
    # surrogate pairs, a literal interior BOM, and combining characters.
    text = "a" * 98 + "你😀好\ufeffe\u0301\n尾"
    raw = encoded(text, encoding)
    (root / "text.txt").write_bytes(raw)
    offset, pages, consumed = 0, [], 0
    while True:
        result = await ReadFileTool().execute({
            "path": "text.txt", "encoding": "utf-16" if encoding.startswith("utf-16") else encoding,
            "offset": offset, "limit": limit,
        }, context, db=object())
        assert result.success, result.error
        pages.append(result.data["content"])
        evidence = result.evidence[0]
        assert evidence["byte_offset"] == offset
        assert 0 < evidence["byte_length"] <= limit + 3
        consumed += evidence["byte_length"]
        assert result.data["checksum"] == hashlib.sha256(raw).hexdigest()
        if not result.data["truncated"]:
            assert result.data["next_offset"] is None
            break
        assert result.data["next_offset"] == consumed > offset
        offset = result.data["next_offset"]
    assert "".join(pages) == text
    assert consumed == len(raw)


@pytest.mark.parametrize("encoding,offset", [
    ("utf-8", 2), ("utf-8", 3), ("utf-8", 5), ("utf-8", 7),
    ("utf-16-le", 1), ("utf-16-le", 3), ("utf-16-le", 8),
    ("utf-16-be", 5), ("utf-16-be", 8), ("utf-16-be", 9),
])
async def test_arbitrary_offsets_inside_characters_are_rejected(file_tools, encoding, offset):
    root, context = file_tools
    (root / "text.txt").write_bytes(encoded("A你😀Z", encoding))
    result = await ReadFileTool().execute({
        "path": "text.txt", "offset": offset,
        "encoding": "utf-16" if encoding.startswith("utf-16") else encoding,
    }, context, db=object())
    assert result.error.code == "workspace_offset_invalid"
    assert result.error.category == "validation"
    assert not result.retryable


@pytest.mark.parametrize("encoding,offset", [
    ("utf-8", 1), ("utf-16-le", 4), ("utf-16-be", 4),
])
async def test_arbitrary_character_boundary_offsets_are_exact(file_tools, encoding, offset):
    root, context = file_tools
    (root / "text.txt").write_bytes(encoded("A你😀Z", encoding))
    result = await ReadFileTool().execute({
        "path": "text.txt", "offset": offset,
        "encoding": "utf-16" if encoding.startswith("utf-16") else encoding,
    }, context, db=object())
    assert result.success
    assert result.data["content"] == "你😀Z"
    assert result.evidence[0]["byte_offset"] == offset


@pytest.mark.parametrize("encoding,raw", [
    ("utf-8", b"abc\xe4\xb8"),
    ("utf-8", b"abc\xff"),
    ("utf-16", codecs.BOM_UTF16_LE + b"A\x00\x00"),
    ("utf-16", codecs.BOM_UTF16_BE + b"\xd8\x00"),
    ("utf-16", b"A\x00"),
])
async def test_invalid_encoding_has_typed_nonretryable_error(file_tools, encoding, raw):
    root, context = file_tools
    (root / "text.txt").write_bytes(raw)
    offset = 0
    while True:
        result = await ReadFileTool().execute({
            "path": "text.txt", "encoding": encoding, "limit": 4, "offset": offset,
        }, context, db=object())
        if not result.success:
            break
        # A bounded valid prefix need not inspect a malformed later page.
        assert result.data["next_offset"] is not None
        assert result.data["next_offset"] > offset
        offset = result.data["next_offset"]
    assert result.error.code == "workspace_encoding_error"
    assert result.error.category == "validation"
    assert not result.retryable


async def test_missing_file_has_typed_nonretryable_error(file_tools):
    _, context = file_tools
    result = await ReadFileTool().execute({"path": "missing"}, context, db=object())
    assert result.error.code == "workspace_file_not_found"
    assert result.error.category == "validation"
    assert not result.retryable


@pytest.mark.parametrize("raw,encoding,offset", [
    (b"", "utf-8", 0), (b"", "utf-16", 0),
    (b"abc", "utf-8", 3), (b"abc", "utf-8", 100),
    (codecs.BOM_UTF16_LE, "utf-16", 0),
])
async def test_empty_and_eof_pages_remain_successful(file_tools, raw, encoding, offset):
    root, context = file_tools
    (root / "text.txt").write_bytes(raw)
    result = await ReadFileTool().execute({
        "path": "text.txt", "encoding": encoding, "offset": offset, "limit": 1,
    }, context, db=object())
    assert result.success
    assert result.data["content"] == ""
    assert result.data["next_offset"] is None
    assert not result.data["truncated"]


def test_latin1_preserves_exact_bytes_at_arbitrary_offset():
    content, consumed = _read_text_page(
        io.BytesIO(bytes(range(256))), offset=127, limit=8, encoding="latin-1",
    )
    assert content.encode("latin-1") == bytes(range(127, 135))
    assert consumed == 8


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be"])
def test_page_io_is_bounded_independently_of_file_size(encoding):
    class BoundedReader(io.BytesIO):
        read_bytes = 0

        def read(self, size=-1):
            assert 0 <= size <= 100, "Page reads must have a finite bound"
            result = super().read(size)
            self.read_bytes += len(result)
            assert self.read_bytes <= 105  # page + scalar completion + UTF-16 BOM probe
            return result

    stream = BoundedReader(encoded("你😀" * 10000, encoding))
    content, consumed = _read_text_page(
        stream, offset=0, limit=100,
        encoding="utf-16" if encoding.startswith("utf-16") else encoding,
    )
    assert content
    assert consumed <= 103


@pytest.mark.parametrize("mode,exists,checksum,error_code", [
    ("create", True, None, "workspace_target_exists"),
    ("replace", False, "0" * 64, "workspace_target_missing"),
    ("replace", True, "0" * 64, "workspace_checksum_conflict"),
    ("replace", True, None, "workspace_checksum_required"),
])
async def test_write_conflicts_do_not_retry_or_modify_files(
    file_tools, mode, exists, checksum, error_code,
):
    root, context = file_tools
    path = root / "text.txt"
    if exists:
        path.write_bytes(b"original")
    arguments = {"path": "text.txt", "mode": mode, "content": "changed"}
    if checksum is not None:
        arguments["expected_checksum"] = checksum
    result = await WriteFileTool().execute(arguments, context, db=object())
    assert result.error.code == error_code
    assert not result.retryable
    assert path.exists() == exists
    if exists:
        assert path.read_bytes() == b"original"


async def test_write_can_recover_after_refreshing_checksum(file_tools):
    root, context = file_tools
    path = root / "text.txt"
    path.write_text("original", encoding="utf-8")
    read = await ReadFileTool().execute({"path": "text.txt"}, context, db=object())
    result = await WriteFileTool().execute({
        "path": "text.txt", "mode": "replace", "content": "更新😀",
        "expected_checksum": read.data["checksum"],
    }, context, db=object())
    assert result.success
    assert path.read_text(encoding="utf-8") == "更新😀"
    assert result.data["checksum"] == hashlib.sha256(path.read_bytes()).hexdigest()
