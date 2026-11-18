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

"""Retain two legacy operations through the resource's authenticated SDK client.

RequestInfo contracts adapted from foundry-platform-sdk 1.79.0, commit
2da67907be429c35f747eef565867ce81dd2cafc,
foundry_sdk/v2/ontologies/geotemporal_series_property.py. Changes: reuse the
existing resource transport, extract the two methods and add native-first
selection. The complete upstream license is LICENSE.foundry-sdk.

SDK 1.101.0 and 1.111.0 omit these methods. Their different loadEntries operation
does not implement the retained contracts. The private _api_client dependency
is deliberately confined here and covered by native SDK wire tests.
"""

from typing import Any, Awaitable, Callable, Optional, cast

import pydantic
from foundry_sdk import _core as core
from foundry_sdk import _errors as errors

from pal_found_cli.ontologies import _legacy_geotemporal_models as legacy

_PREFIX = (
    "/v2/ontologies/{ontology}/objects/{objectType}/{primaryKey}"
    "/geotemporalSeries/{propertyName}/"
)
_OPERATIONS = frozenset(
    {"get_geotemporal_series_latest_value", "stream_geotemporal_series_historic_values"}
)


def resolve_method(client: Any, operation: str) -> Callable[..., Any]:
    """Return the native method, falling back only for the two retained names."""
    method = getattr(client, operation, None)
    if method is not None:
        return cast(Callable[..., Any], method)
    if operation not in _OPERATIONS:
        raise AttributeError(f"SDK resource has no operation {operation!r}")
    return cast(
        Callable[..., Any], getattr(_LegacyGeotemporalClient(client._api_client), operation)
    )


class _LegacyGeotemporalClient:
    def __init__(self, api_client: core.AsyncApiClient) -> None:
        self._api_client = api_client

    @core.maybe_ignore_preview
    @pydantic.validate_call
    @errors.handle_unexpected
    def get_geotemporal_series_latest_value(
        self,
        ontology: legacy.OntologyIdentifier,
        object_type: legacy.ObjectTypeApiName,
        primary_key: legacy.PropertyValueEscapedString,
        property_name: legacy.PropertyApiName,
        *,
        sdk_package_rid: Optional[legacy.SdkPackageRid] = None,
        sdk_version: Optional[legacy.SdkVersion] = None,
        request_timeout: Optional[core.Timeout] = None,
    ) -> Awaitable[Optional[legacy.GeotemporalSeriesEntry]]:
        response = self._api_client.call_api(
            core.RequestInfo(
                method="GET",
                resource_path=_PREFIX + "latestValue",
                query_params={
                    "sdkPackageRid": sdk_package_rid,
                    "sdkVersion": sdk_version,
                },
                path_params={
                    "ontology": ontology,
                    "objectType": object_type,
                    "primaryKey": primary_key,
                    "propertyName": property_name,
                },
                header_params={"Accept": "application/json"},
                body=None,
                response_type=Optional[legacy.GeotemporalSeriesEntry],
                request_timeout=request_timeout,
                throwable_errors={},
            )
        )
        return cast(Awaitable[Optional[legacy.GeotemporalSeriesEntry]], response)

    @core.maybe_ignore_preview
    @pydantic.validate_call
    @errors.handle_unexpected
    def stream_geotemporal_series_historic_values(
        self,
        ontology: legacy.OntologyIdentifier,
        object_type: legacy.ObjectTypeApiName,
        primary_key: legacy.PropertyValueEscapedString,
        property_name: legacy.PropertyApiName,
        *,
        range: Optional[legacy.TimeRange] = None,
        sdk_package_rid: Optional[legacy.SdkPackageRid] = None,
        sdk_version: Optional[legacy.SdkVersion] = None,
        request_timeout: Optional[core.Timeout] = None,
    ) -> Awaitable[bytes]:
        response = self._api_client.call_api(
            core.RequestInfo(
                method="POST",
                resource_path=_PREFIX + "streamHistoricValues",
                query_params={
                    "sdkPackageRid": sdk_package_rid,
                    "sdkVersion": sdk_version,
                },
                path_params={
                    "ontology": ontology,
                    "objectType": object_type,
                    "primaryKey": primary_key,
                    "propertyName": property_name,
                },
                header_params={"Content-Type": "application/json", "Accept": "*/*"},
                body=legacy.StreamGeotemporalSeriesValuesRequest(range=range),
                response_type=bytes,
                request_timeout=request_timeout,
                throwable_errors={},
            )
        )
        return cast(Awaitable[bytes], response)
