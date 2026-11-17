# Copyright 2024 Palantir Technologies, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Minimal models for the two retained legacy Geotemporal CLI operations.

Adapted from foundry-platform-sdk 1.79.0, commit
2da67907be429c35f747eef565867ce81dd2cafc, v2/ontologies/models.py and
v2/geo/models.py. Changes: extract the required closure into one module,
resolve local union references directly, and retain the legacy field contracts.
The complete upstream license is distributed as LICENSE.foundry-sdk.
"""

from __future__ import annotations

import typing

import annotated_types
import pydantic
from foundry_sdk import _core as core

Coordinate = float
BBox = typing.List[Coordinate]
Position = typing.Annotated[
    typing.List[Coordinate], annotated_types.Len(min_length=2, max_length=3)
]


class GeoPoint(core.ModelBase):
    """Legacy GeoJSON point with two or three coordinates."""

    coordinates: Position
    bbox: typing.Optional[BBox] = None
    type: typing.Literal["Point"] = "Point"


class GeotemporalSeriesEntry(core.ModelBase):
    """One recorded location at an aware timestamp."""

    time: core.AwareDatetime
    position: GeoPoint


class AbsoluteTimeRange(core.ModelBase):
    """Inclusive start and exclusive end; either boundary may be omitted."""

    start_time: typing.Optional[core.AwareDatetime] = pydantic.Field(
        alias="startTime", default=None
    )
    end_time: typing.Optional[core.AwareDatetime] = pydantic.Field(
        alias="endTime", default=None
    )
    type: typing.Literal["absolute"] = "absolute"


RelativeTimeRelation = typing.Literal["BEFORE", "AFTER"]
RelativeTimeSeriesTimeUnit = typing.Literal[
    "MILLISECONDS", "SECONDS", "MINUTES", "HOURS", "DAYS", "WEEKS", "MONTHS", "YEARS"
]


class RelativeTime(core.ModelBase):
    """A relative offset from the current time."""

    when: RelativeTimeRelation
    value: int
    unit: RelativeTimeSeriesTimeUnit


class RelativeTimeRange(core.ModelBase):
    """Legacy relative range with optional boundaries."""

    start_time: typing.Optional[RelativeTime] = pydantic.Field(
        alias="startTime", default=None
    )
    end_time: typing.Optional[RelativeTime] = pydantic.Field(
        alias="endTime", default=None
    )
    type: typing.Literal["relative"] = "relative"


TimeRange = typing.Annotated[
    typing.Union[AbsoluteTimeRange, RelativeTimeRange],
    pydantic.Field(discriminator="type"),
]


class StreamGeotemporalSeriesValuesRequest(core.ModelBase):
    """Legacy historic-values request body."""

    range: typing.Optional[TimeRange] = None


OntologyIdentifier = str
ObjectTypeApiName = str
PropertyValueEscapedString = str
PropertyApiName = str
SdkPackageRid = core.RID
SdkVersion = str
