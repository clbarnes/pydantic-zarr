from __future__ import annotations

from typing import (
    Any,
    Generic,
    Literal,
    Mapping,
    Optional,
    TypeVar,
    Union,
)
from pydantic import root_validator

from pydantic.generics import GenericModel
from zarr.storage import init_group, BaseStore
import numcodecs
import zarr
import os

TAttrs = TypeVar("TAttrs", bound=Mapping[str, Any])
TItem = TypeVar("TItem", bound=Union["GroupSpec", "ArraySpec"])

DimensionSeparator = Union[Literal["."], Literal["/"]]
ZarrVersion = Union[Literal[2], Literal[3]]
ArrayOrder = Union[Literal["C"], Literal["F"]]


class NodeSpec(GenericModel, Generic[TAttrs]):
    """
    The base class for ArraySpec and GroupSpec. Generic with respect to the type of
    attrs.
    """

    zarr_version: ZarrVersion = 2
    attrs: TAttrs

    class Config:
        extra = "forbid"


class ArraySpec(NodeSpec, Generic[TAttrs]):
    """
    This pydantic model represents the structural properties of a zarr array.
    It does not represent the data contained in the array. It is generic with respect to
    the type of attrs.
    """

    shape: tuple[int, ...]
    chunks: tuple[int, ...]
    dtype: str
    fill_value: Union[None, int, float] = 0
    order: ArrayOrder = "C"
    filters: Optional[list[dict[str, Any]]] = None
    dimension_separator: DimensionSeparator = "/"
    compressor: Optional[dict[str, Any]] = None

    @root_validator
    def check_ndim(cls, values):
        if (lshape := len(values["shape"])) != (lchunks := len(values["chunks"])):
            msg = f"""
            Length of shape must match length of chunks. Got {lshape} elements
            for shape and {lchunks} elements for chunks.
            """
            raise ValueError(msg)
        return values

    @classmethod
    def from_zarr(cls, zarray: zarr.Array):
        """
        Create an ArraySpec from a zarr array.

        Parameters
        ----------
        zarray : zarr array

        Returns
        -------
        An instance of ArraySpec with properties derived from the provided zarr
        array.

        """

        filters = zarray.filters
        if filters is not None:
            filters = [f.get_config() for f in filters]

        compressor = zarray.compressor
        if compressor is not None:
            compressor = compressor.get_config()
        return cls(
            shape=zarray.shape,
            chunks=zarray.chunks,
            dtype=str(zarray.dtype),
            fill_value=zarray.fill_value,
            order=zarray.order,
            filters=filters,
            dimension_separator=zarray._dimension_separator,
            compressor=compressor,
            attrs=dict(zarray.attrs),
        )

    def to_zarr(
        self, store: BaseStore, path: str, overwrite: bool = False
    ) -> zarr.Array:
        """
        Serialize an ArraySpec to a zarr array at a specific path in a zarr store.

        Parameters
        ----------
        store : instance of zarr.BaseStore
            The storage backend that will manifest the array.

        path : str
            The location of the array inside the store.

        overwrite : bool
            Whether to overwrite an existing array or group at the path. If overwrite is
            False and an array or group already exists at the path, an exception will be
            raised. Defaults to False.

        Returns
        -------
        A zarr array that is structurally identical to the ArraySpec.
        This operation will create metadata documents in the store.
        """
        spec_dict = self.dict()
        attrs = spec_dict.pop("attrs")
        if self.compressor is not None:
            spec_dict["compressor"] = numcodecs.get_codec(spec_dict["compressor"])
        if self.filters is not None:
            spec_dict["filters"] = [
                numcodecs.get_codec(f) for f in spec_dict["filters"]
            ]
        result = zarr.create(store=store, path=path, **spec_dict, overwrite=overwrite)
        result.attrs.put(attrs)
        return result


class GroupSpec(NodeSpec, Generic[TAttrs, TItem]):
    items: dict[str, TItem] = {}

    @classmethod
    def from_zarr(cls, zgroup: zarr.Group) -> "GroupSpec":
        """
        Create a GroupSpec from a zarr group. Subgroups and arrays contained in the zarr
        group will be converted to instances of GroupSpec and ArraySpec, respectively,
        and these spec instances will be stored in the .items attribute of the parent
        GroupSpec. This occurs recursively, so the entire zarr hierarchy below a given
        group can be represented as a GroupSpec.

        Parameters
        ----------
        zgroup : zarr group

        Returns
        -------
        An instance of GroupSpec that represents the structure of the zarr hierarchy.
        """
        result: GroupSpec
        items = {}
        for name, item in zgroup.items():
            if isinstance(item, zarr.Array):
                _item = ArraySpec.from_zarr(item)
            elif isinstance(item, zarr.Group):
                _item = cls.from_zarr(item)
            else:
                msg = f"""
                Unparseable object encountered: {type(item)}. Expected zarr.Array or
                zarr.Group.
                """
                raise ValueError(msg)
            items[name] = _item

        result = GroupSpec(attrs=dict(zgroup.attrs), items=items)
        return result

    def to_zarr(self, store: BaseStore, path: str, overwrite: bool = False):
        """
        Serialize a GroupSpec to a zarr group at a specific path in a zarr store.

        Parameters
        ----------
        store : instance of zarr.BaseStore
            The storage backend that will manifest the group and its contents.

        path : str
            The location of the group inside the store.

        overwrite : bool
            Whether to overwrite an existing array or group at the path. If overwrite is
            False and an array or group already exists at the path, an exception will be
            raised. Defaults to False.

        Returns
        -------
        A zarr group that is structurally identical to the GroupSpec.
        This operation will create metadata documents in the store.
        """
        spec_dict = self.dict()
        # pop items because it's not a valid kwarg for init_group
        spec_dict.pop("items")
        # pop attrs because it's not a valid kwarg for init_group
        attrs = spec_dict.pop("attrs")
        # weird that we have to call init_group before creating the group
        init_group(store, overwrite=overwrite, path=path)
        result = zarr.group(store=store, path=path, **spec_dict, overwrite=overwrite)
        result.attrs.put(attrs)
        for name, item in self.items.items():
            subpath = os.path.join(path, name)
            item.to_zarr(store, subpath, overwrite=overwrite)

        return result


def from_zarr(element: Union[zarr.Array, zarr.Group]) -> Union[ArraySpec, GroupSpec]:
    """
    Recursively parse a Zarr group or Zarr array into an ArraySpec or GroupSpec.

    Paramters
    ---------
    element : a zarr Array or zarr Group

    Returns
    -------
    An instance of GroupSpec or ArraySpec that represents the
    structure of the zarr group or array.
    """

    if isinstance(element, zarr.Array):
        result = ArraySpec.from_zarr(element)
    elif isinstance(element, zarr.Group):
        items = {}
        for name, item in element.items():
            if isinstance(item, zarr.Array):
                _item = ArraySpec.from_zarr(item)
            elif isinstance(item, zarr.Group):
                _item = GroupSpec.from_zarr(item)
            else:
                msg = f"""
                Unparseable object encountered: {type(item)}. Expected zarr.Array or
                zarr.Group.
                """
                raise ValueError(msg)
            items[name] = _item

        result = GroupSpec(attrs=dict(element.attrs), items=items)
        return result
    else:
        msg = f"""
        Object of type {type(element)} cannot be processed by this function. 
        This function can only parse objects that comply with the ArrayLike or 
        GroupLike protocols.
        """
        raise ValueError(msg)
    return result


def to_zarr(
    spec: Union[ArraySpec, GroupSpec],
    store: BaseStore,
    path: str,
    overwrite: bool = False,
) -> Union[zarr.Array, zarr.Group]:
    """
    Serialize a GroupSpec or ArraySpec to a zarr group or array at a specific path in
    a zarr store.

    Parameters
    ----------
    spec : GroupSpec or ArraySpec
        The GroupSpec or ArraySpec that will be serialized to storage.

    store : instance of zarr.BaseStore
        The storage backend that will manifest the group or array.

    path : str
        The location of the group or array inside the store.

    overwrite : bool
       Whether to overwrite an existing array or group at the path. If overwrite is
        False and an array or group already exists at the path, an exception will be
        raised. Defaults to False.

    Returns
    -------
    A zarr Group or Array that is structurally identical to the spec object.
    This operation will create metadata documents in the store.

    """
    if isinstance(spec, ArraySpec):
        result = spec.to_zarr(store, path, overwrite=overwrite)
    elif isinstance(spec, GroupSpec):
        result = spec.to_zarr(store, path, overwrite=overwrite)
    else:
        msg = f"""
        Invalid argument for spec. Expected an instance of GroupSpec or ArraySpec, got
        {type(spec)} instead.
        """
        raise ValueError(msg)

    return result
