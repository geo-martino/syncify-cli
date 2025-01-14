"""
Configures Pydantic types to use as annotations in models.
"""
from collections.abc import Collection
from functools import partial
from typing import Annotated

from aiorequestful.types import UnitSequence
from musify.types import MusifyEnum
from pydantic import BeforeValidator


def from_names[T: MusifyEnum](names: Collection[str | T], cls: type[T]) -> Collection[T]:
    """Get a list of :py:class:`.MusifyEnum` from the given enum ``names``"""
    if all(name is cls for name in names):
        return names
    return cls.from_name(*names, fail_on_many=False)


class LoadTypesLocal(MusifyEnum):
    TRACKS = 0
    PLAYLISTS = 1


LoadTypesLocalAnno = Annotated[
    UnitSequence[LoadTypesLocal], BeforeValidator(partial(from_names, cls=LoadTypesLocal))
]


class LoadTypesRemote(MusifyEnum):
    PLAYLISTS = 1
    SAVED_TRACKS = 10
    SAVED_ALBUMS = 11
    SAVED_ARTISTS = 12


LoadTypesRemoteAnno = Annotated[
    UnitSequence[LoadTypesRemote], BeforeValidator(partial(from_names, cls=LoadTypesRemote))
]


class EnrichTypesRemote(MusifyEnum):
    TRACKS = 0
    ALBUMS = 1
    ARTISTS = 2


EnrichTypesRemoteAnno = Annotated[
    UnitSequence[EnrichTypesRemote], BeforeValidator(partial(from_names, cls=EnrichTypesRemote))
]
