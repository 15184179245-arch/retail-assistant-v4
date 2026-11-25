"""Protected parsed category artifact; integrity is not business authorization.

The path and expected artifact SHA come only from trusted Host configuration.
The content SHA is preserved from the earlier directory extraction: this parsed
artifact cannot reproduce the original flat/tree response semantic digest.
"""
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat

from .category_directory import Category, CategoryDirectory, MAX_CATEGORIES

MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_BYTES = 32 * 1024


class CategorySourceLoadError(ValueError):
    def __init__(self):
        super().__init__('guanjia_category_source_unavailable')


def _deny():
    raise CategorySourceLoadError()


def _private(info, *, directory=False):
    if (info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        _deny()


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_nlink)


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _deny()
            result[key] = value
        return result
    def constant(value):
        _deny()
    if raw.startswith(b'\xef\xbb\xbf'):
        _deny()
    value = json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=pairs,
                       parse_constant=constant)
    if type(value) is not dict:
        _deny()
    return value


def _shape(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        _deny()


def _text(value):
    if (type(value) is not str or not 0 < len(value) <= 200
            or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 or 0xd800 <= ord(c) <= 0xdfff for c in value)):
        _deny()
    return value


def _digest(value):
    if type(value) is not str or re.fullmatch('[a-f0-9]{64}', value) is None:
        _deny()
    return value


def _read(rootfd, name, maximum):
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=rootfd)
        before = os.fstat(fd)
        _private(before)
        if not 0 < before.st_size <= maximum:
            _deny()
        chunks, remaining = [], maximum + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(chunks)
        if len(raw) != before.st_size or len(raw) > maximum:
            _deny()
        _stable(rootfd, name, fd, before)
        return raw, fd, before
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise


def _stable(rootfd, name, fd, before):
    named = os.stat(name, dir_fd=rootfd, follow_symlinks=False)
    _private(named)
    if _stamp(before) != _stamp(os.fstat(fd)) or _stamp(before) != _stamp(named):
        _deny()


@dataclass(frozen=True)
class LoadedCategorySource:
    directory: CategoryDirectory
    artifact_sha256: str
    content_sha256: str


_RESULT_KEYS = ('category_count', 'category_ids_unique', 'content_sha256',
    'feed_product_count', 'feed_unknown_category_count', 'known_scope_match_count',
    'known_scope_product_count', 'note', 'observed_at', 'requests', 'status', 'upstream_writes')


def _result(result, count):
    _shape(result, _RESULT_KEYS)
    for key in ('category_count', 'feed_product_count', 'feed_unknown_category_count',
                'known_scope_match_count', 'known_scope_product_count', 'upstream_writes'):
        if type(result[key]) is not int or not 0 <= result[key] <= 2**63 - 1:
            _deny()
    if (result['category_count'] != count or result['category_ids_unique'] is not True
            or result['feed_unknown_category_count'] != 0 or result['upstream_writes'] != 0
            or result['status'] != 'complete'):
        _deny()
    _digest(result['content_sha256'])
    if (type(result['note']) is not str or len(result['note']) > 4096
            or any(0xd800 <= ord(c) <= 0xdfff for c in result['note'])
            or type(result['observed_at']) is not str or len(result['observed_at']) > 64):
        _deny()
    _shape(result['requests'], ('authenticate', 'category_flat', 'category_tree', 'product_page'))
    if any(type(v) is not int or not 0 <= v <= 2**63 - 1 for v in result['requests'].values()):
        _deny()


def _directory(value):
    _shape(value, ('categories', 'result'))
    if type(value['categories']) is not list:
        _deny()
    if not 0 <= len(value['categories']) <= MAX_CATEGORIES:
        _deny()
    result = value['result']
    _result(result, len(value['categories']))
    digest = _digest(result['content_sha256'])
    observed = datetime.fromisoformat(result['observed_at'])
    if observed.tzinfo is None or observed.utcoffset() is None:
        _deny()
    categories, by_id = [], {}
    for row in value['categories']:
        _shape(row, ('business_id', 'name', 'code', 'parent_business_id', 'path'))
        identity = _text(row['business_id'])
        if identity == '0' or identity in by_id:
            _deny()
        parent = row['parent_business_id']
        if parent is not None:
            _text(parent)
        if type(row['path']) is not list or not row['path']:
            _deny()
        category = Category(identity, _text(row['name']), _text(row['code']), parent,
                            tuple(_text(item) for item in row['path']))
        by_id[identity] = category
        categories.append(category)
    for category in categories:
        chain, seen, current = [], set(), category
        while current is not None:
            if current.business_id in seen:
                _deny()
            seen.add(current.business_id)
            chain.append(current.name)
            parent = current.parent_business_id
            if parent is not None and parent not in by_id:
                _deny()
            current = by_id[parent] if parent is not None else None
        if category.path != tuple(reversed(chain)):
            _deny()
    return CategoryDirectory(tuple(categories), digest, observed)


def load_category_source(directory_path: Path, expected_artifact_sha256: str) -> LoadedCategorySource:
    """Read fixed names beneath a trusted private directory; never writes files."""
    rootfd = None
    opened = []
    try:
        expected = _digest(expected_artifact_sha256)
        if not isinstance(directory_path, Path):
            _deny()
        root = directory_path
        if not root.is_absolute() or '..' in root.parts or root.resolve(strict=True) != root:
            _deny()
        rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        rootinfo = os.fstat(rootfd)
        _private(rootinfo, directory=True)
        raw, fd, info = _read(rootfd, 'category-directory.json', MAX_ARTIFACT_BYTES)
        opened.append(('category-directory.json', fd, info))
        if sha256(raw).hexdigest() != expected:
            _deny()
        receipt_raw, fd, info = _read(rootfd, 'category-result.json', MAX_RECEIPT_BYTES)
        opened.append(('category-result.json', fd, info))
        directory = _directory(_decode(raw))
        receipt = _decode(receipt_raw)
        _shape(receipt, (*_RESULT_KEYS, 'artifact_sha256'))
        _result({key: receipt[key] for key in _RESULT_KEYS}, len(directory.categories))
        if (receipt['artifact_sha256'] != expected
                or {key: receipt[key] for key in _RESULT_KEYS} != _decode(raw)['result']):
            _deny()
        for name, fd, info in opened:
            _stable(rootfd, name, fd, info)
        named_root = os.stat(root, follow_symlinks=False)
        _private(named_root, directory=True)
        if (_stamp(rootinfo) != _stamp(os.fstat(rootfd))
                or _stamp(rootinfo) != _stamp(named_root) or root.resolve(strict=True) != root):
            _deny()
        return LoadedCategorySource(directory, expected, directory.content_sha256)
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError, UnicodeError):
        raise CategorySourceLoadError() from None
    finally:
        for _, fd, _ in opened:
            os.close(fd)
        if rootfd is not None:
            os.close(rootfd)
