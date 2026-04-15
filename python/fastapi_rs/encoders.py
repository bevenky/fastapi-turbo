"""jsonable_encoder — convert objects to JSON-serializable format.

Compatible with ``fastapi.encoders.jsonable_encoder``.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import uuid
from pathlib import PurePath
from typing import Any, Dict, List, Set, Tuple, Type


def jsonable_encoder(
    obj: Any,
    *,
    include: set | dict | None = None,
    exclude: set | dict | None = None,
    by_alias: bool = True,
    exclude_unset: bool = False,
    exclude_defaults: bool = False,
    exclude_none: bool = False,
    custom_encoder: dict[Type, Any] | None = None,
    sqlalchemy_safe: bool = True,
) -> Any:
    """Convert an object to a JSON-serializable format.

    Handles Pydantic models, dataclasses, dicts, lists, enums,
    datetimes, UUIDs, Decimals, Paths, etc.
    """
    custom_encoder = custom_encoder or {}

    # Check custom encoder first
    for encoder_type, encoder_func in custom_encoder.items():
        if isinstance(obj, encoder_type):
            return encoder_func(obj)

    # Pydantic BaseModel
    try:
        from pydantic import BaseModel

        if isinstance(obj, BaseModel):
            # Pydantic v2
            if hasattr(obj, "model_dump"):
                dump_kwargs: dict[str, Any] = {"by_alias": by_alias}
                if include is not None:
                    dump_kwargs["include"] = include
                if exclude is not None:
                    dump_kwargs["exclude"] = exclude
                if exclude_unset:
                    dump_kwargs["exclude_unset"] = True
                if exclude_defaults:
                    dump_kwargs["exclude_defaults"] = True
                if exclude_none:
                    dump_kwargs["exclude_none"] = True
                data = obj.model_dump(**dump_kwargs)
            else:
                # Pydantic v1 fallback
                data = obj.dict(
                    include=include,
                    exclude=exclude,
                    by_alias=by_alias,
                    exclude_unset=exclude_unset,
                    exclude_defaults=exclude_defaults,
                    exclude_none=exclude_none,
                )
            return jsonable_encoder(data, custom_encoder=custom_encoder)
    except ImportError:
        pass

    # Dataclasses
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        data = dataclasses.asdict(obj)
        return jsonable_encoder(
            data,
            include=include,
            exclude=exclude,
            exclude_none=exclude_none,
            custom_encoder=custom_encoder,
        )

    # Enum
    if isinstance(obj, enum.Enum):
        return obj.value

    # PurePath (Path, PosixPath, WindowsPath, etc.)
    if isinstance(obj, PurePath):
        return str(obj)

    # Primitives
    if isinstance(obj, (str, int, float, type(None))):
        return obj

    # bool must be checked before int (bool is subclass of int)
    if isinstance(obj, bool):
        return obj

    # Dict
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if exclude and k in exclude:
                continue
            if include and k not in include:
                continue
            if exclude_none and v is None:
                continue
            # SQLAlchemy safety: skip keys starting with _sa_
            if sqlalchemy_safe and isinstance(k, str) and k.startswith("_sa_"):
                continue
            result[str(k)] = jsonable_encoder(
                v,
                exclude_none=exclude_none,
                custom_encoder=custom_encoder,
            )
        return result

    # Sequences (list, tuple, set, frozenset, deque, etc.)
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [
            jsonable_encoder(
                item,
                exclude_none=exclude_none,
                custom_encoder=custom_encoder,
            )
            for item in obj
        ]

    # Bytes
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")

    # Datetime types
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()

    if isinstance(obj, datetime.date):
        return obj.isoformat()

    if isinstance(obj, datetime.time):
        return obj.isoformat()

    if isinstance(obj, datetime.timedelta):
        return obj.total_seconds()

    # UUID
    if isinstance(obj, uuid.UUID):
        return str(obj)

    # Decimal
    if isinstance(obj, decimal.Decimal):
        # Return as float if it can be represented exactly, else string
        if obj == obj.to_integral_value():
            return int(obj)
        return float(obj)

    # Generators / iterables
    try:
        return [
            jsonable_encoder(
                item,
                exclude_none=exclude_none,
                custom_encoder=custom_encoder,
            )
            for item in obj
        ]
    except TypeError:
        pass

    # Objects with __dict__
    if hasattr(obj, "__dict__"):
        data = {}
        for k, v in obj.__dict__.items():
            if sqlalchemy_safe and k.startswith("_sa_"):
                continue
            if exclude_none and v is None:
                continue
            data[k] = jsonable_encoder(
                v,
                exclude_none=exclude_none,
                custom_encoder=custom_encoder,
            )
        return data

    # Fallback: convert to string
    return str(obj)
