"""Lossless private replay values, preserving type, float bits and mapping order.

Only plain values are accepted. This does not introspect live gameplay objects,
invoke user serializers or represent shared object identity.
"""

import math
import struct

MAX_DEPTH = 128
MAX_VALUES = 1_000_000


class ReplayValueError(ValueError):
    pass


def encode(value):
    ancestors = set()
    count = 0

    def visit(item, depth):
        nonlocal count
        count += 1
        if depth > MAX_DEPTH or count > MAX_VALUES:
            raise ReplayValueError("replay value exceeds structural budget")
        kind = type(item)
        if item is None:
            return ["none"]
        if kind is bool:
            return ["bool", item]
        if kind is int:
            return ["int", hex(item)]
        if kind is float:
            if not math.isfinite(item):
                raise ReplayValueError("nonfinite replay float")
            return ["float", struct.pack(">d", item).hex()]
        if kind is str:
            return ["str", item]
        if kind is bytes:
            return ["bytes", item.hex()]
        if kind not in (tuple, list, dict):
            raise ReplayValueError("unsupported replay value type")
        identity = id(item)
        if identity in ancestors:
            raise ReplayValueError("cyclic replay value")
        ancestors.add(identity)
        try:
            if kind is dict:
                pairs = []
                for key, nested in item.items():
                    if type(key) not in (str, int):
                        raise ReplayValueError("replay mapping key must be a string or integer")
                    pairs.append([visit(key, depth + 1), visit(nested, depth + 1)])
                return ["dict", pairs]
            return ["tuple" if kind is tuple else "list", [visit(v, depth + 1) for v in item]]
        finally:
            ancestors.remove(identity)

    return visit(value, 0)


def decode(value):
    count = 0
    ancestors = set()

    def visit(row, depth):
        nonlocal count
        count += 1
        if depth > MAX_DEPTH or count > MAX_VALUES:
            raise ReplayValueError("replay value exceeds structural budget")
        if type(row) is not list or not row or type(row[0]) is not str:
            raise ReplayValueError("invalid typed replay row")
        tag = row[0]
        if tag == "none":
            if len(row) != 1:
                raise ReplayValueError("invalid null replay row")
            return None
        if len(row) != 2:
            raise ReplayValueError("invalid typed replay row length")
        data = row[1]
        if tag == "bool" and type(data) is bool:
            return data
        if tag == "str" and type(data) is str:
            return data
        if tag in ("int", "float", "bytes") and type(data) is str:
            try:
                if tag == "int":
                    result = int(data, 16)
                elif tag == "float":
                    if len(data) != 16:
                        raise ValueError
                    result = struct.unpack(">d", bytes.fromhex(data))[0]
                    if not math.isfinite(result):
                        raise ValueError
                else:
                    result = bytes.fromhex(data)
                if encode(result) != row:
                    raise ValueError
                return result
            except (ValueError, struct.error):
                raise ReplayValueError("noncanonical replay scalar") from None
        if tag not in ("tuple", "list", "dict") or type(data) is not list:
            raise ReplayValueError("invalid replay type or value")
        identity = id(row)
        if identity in ancestors:
            raise ReplayValueError("cyclic encoded replay value")
        ancestors.add(identity)
        try:
            if tag == "dict":
                result = {}
                for pair in data:
                    if type(pair) is not list or len(pair) != 2:
                        raise ReplayValueError("invalid replay mapping pair")
                    key = visit(pair[0], depth + 1)
                    if type(key) not in (str, int) or key in result:
                        raise ReplayValueError("invalid or duplicate replay mapping key")
                    result[key] = visit(pair[1], depth + 1)
                return result
            result = [visit(v, depth + 1) for v in data]
            return tuple(result) if tag == "tuple" else result
        finally:
            ancestors.remove(identity)

    return visit(value, 0)


def clone(value):
    """Detach plain values without changing their type or ordered mapping entries."""
    return decode(encode(value))
