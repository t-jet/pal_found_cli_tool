"""Ontology CLI pagination against SDK iterator and page response shapes."""

import argparse
import json
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from foundry_sdk._core.resource_iterator import AsyncResourceIterator

from pal_found_cli.ontologies.scripts import pal_found_ontologies_cli as cli


class _RawResponse:
    def __init__(self, page):
        self.page = page

    def decode(self):
        return self.page


class _Item:
    def __init__(self, value):
        self.value = value

    def to_dict(self):
        return {"value": self.value}


def _args(resource, operation):
    spec = cli._spec_for(resource, operation)
    values = {name: f"{name}-value" for name in spec["positional"]}
    values.update(page_size=None, page_token=None)
    return argparse.Namespace(**values)


def _client(resource, operation, pages):
    method_name = cli._spec_for(resource, operation)["method"]
    client = SimpleNamespace()
    if (resource, operation) in cli.ITERATOR_PAGINATED_OPS:
        raw = SimpleNamespace()
        method = AsyncMock(side_effect=[_RawResponse(page) for page in pages])
        setattr(raw, method_name, method)
        client.with_raw_response = raw
        setattr(client, method_name, MagicMock(side_effect=AssertionError("iterator used")))
    else:
        method = AsyncMock(side_effect=pages)
        setattr(client, method_name, method)
    return client, method


def test_pagination_contract_covers_all_sdk_paged_operations():
    assert len(cli.PAGINATED_OPS) == 16
    assert len(cli.ITERATOR_PAGINATED_OPS) == 10
    assert cli.ITERATOR_PAGINATED_OPS <= cli.PAGINATED_OPS


@pytest.mark.parametrize("resource,operation", sorted(cli.PAGINATED_OPS))
@pytest.mark.asyncio
async def test_every_paged_operation_returns_items_and_continuation(resource, operation):
    pages = [
        SimpleNamespace(data=[_Item(1), _Item(2)], next_page_token="second"),
        {"data": [_Item(3)], "nextPageToken": "third"},
    ]
    client, method = _client(resource, operation, pages)
    helper = cli.PaginationHelper(page_size=2, page_token="start", batch_pages=2)

    result = await cli._invoke_paginated(
        resource, operation, client, _args(resource, operation), 17, helper, None
    )

    assert json.loads(json.dumps(cli._model_to_dict(result))) == [
        {"value": 1},
        {"value": 2},
        {"value": 3},
    ]
    assert helper.pages_fetched == 2
    assert helper.total_items == 3
    assert helper.next_page_token == "third"
    assert method.await_count == 2
    assert method.await_args_list[0].kwargs["page_token"] == "start"
    assert method.await_args_list[1].kwargs["page_token"] == "second"
    assert all(call.kwargs["page_size"] == 2 for call in method.await_args_list)
    assert all(call.kwargs["request_timeout"] == 17 for call in method.await_args_list)


@pytest.mark.parametrize(
    "resource,operation",
    [("action_type", "list"), ("ontology_object", "search")],
)
@pytest.mark.asyncio
async def test_empty_page_with_cursor_and_oversized_page_keep_all_records(
    resource, operation
):
    pages = [
        SimpleNamespace(data=[], next_page_token="after-empty"),
        SimpleNamespace(data=[_Item(n) for n in range(5)], next_page_token="more"),
    ]
    client, method = _client(resource, operation, pages)
    helper = cli.PaginationHelper(page_size=2, batch_pages=2)

    result = await cli._invoke_paginated(
        resource, operation, client, _args(resource, operation), None, helper, None
    )

    assert [item.value for item in result] == list(range(5))
    assert helper.pages_fetched == 2
    assert helper.total_items == 5
    assert helper.next_page_token == "more"
    assert method.await_count == 2


@pytest.mark.parametrize(
    "resource,operation",
    [("action_type", "list"), ("ontology_object_set", "load")],
)
@pytest.mark.asyncio
async def test_last_page_stops_batch_and_metadata_omits_cursor(
    resource, operation, capsys
):
    page = SimpleNamespace(data=[_Item(1)], next_page_token=None)
    if resource == "ontology_object_set":
        page = {"data": [_Item(1)], "nextPageToken": None, "totalCount": 1}
    client, method = _client(
        resource, operation, [page]
    )
    helper = cli.PaginationHelper(page_size=2, batch_pages=10)

    result = await cli._invoke_paginated(
        resource, operation, client, _args(resource, operation), None, helper, None
    )
    helper.emit_metadata()
    metadata = json.loads(capsys.readouterr().err.splitlines()[-1])

    assert [item.value for item in result] == [1]
    assert method.await_count == 1
    expected = {"pages_fetched": 1, "total_items": 1, "page_size": 2}
    if resource == "ontology_object_set":
        expected["response_metadata"] = [{"totalCount": 1}]
    assert metadata == expected


@pytest.mark.asyncio
async def test_sdk_iterator_fallback_reads_one_whole_page_at_a_time():
    fetched_tokens = []

    async def fetch_page(*, page_size, next_page_token):
        fetched_tokens.append(next_page_token)
        if next_page_token is None:
            return "second", [_Item(1), _Item(2), _Item(3)]
        return "third", [_Item(4)]

    def list_action_types(ontology, *, page_size, page_token=None, request_timeout=None):
        return AsyncResourceIterator(fetch_page, page_size, page_token)

    client = SimpleNamespace(list=MagicMock(side_effect=list_action_types))
    helper = cli.PaginationHelper(page_size=2, batch_pages=2)

    result = await cli._invoke_paginated(
        "action_type", "list", client, _args("action_type", "list"), None, helper, None
    )

    assert [item.value for item in result] == [1, 2, 3, 4]
    assert fetched_tokens == [None, "second"]
    assert helper.pages_fetched == 2
    assert helper.next_page_token == "third"


@pytest.mark.asyncio
async def test_iterator_without_page_boundary_is_rejected():
    class EmptyIterator:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    client = SimpleNamespace(list=MagicMock(return_value=EmptyIterator()))
    helper = cli.PaginationHelper(page_size=2)

    with pytest.raises(cli.PaginationBoundaryError):
        await cli._invoke_paginated(
            "action_type", "list", client, _args("action_type", "list"), None, helper, None
        )
    assert helper.total_items == 0


@pytest.mark.parametrize(
    "resource,operation,argv",
    [
        ("action_type", "list", ["action-type", "list", "ontology-id"]),
        (
            "ontology_object",
            "search",
            ["ontology-object", "search", "ontology-id", "object-type", "--select", "[]"],
        ),
    ],
)
@pytest.mark.asyncio
async def test_cli_emits_records_and_cursor_metadata(
    resource, operation, argv, monkeypatch, capsys
):
    client, _ = _client(
        resource,
        operation,
        [SimpleNamespace(data=[_Item(1)], next_page_token="continue")],
    )

    class Config:
        timeout_s = 30
        log_level = "INFO"

        def load(self):
            pass

    monkeypatch.setattr(cli, "ConfigLoader", Config)
    monkeypatch.setattr(cli.LogSetup, "configure", lambda **kwargs: None)
    monkeypatch.setattr(cli, "AccessControlGuard", lambda cfg, ns: SimpleNamespace(check=lambda *args: None))
    monkeypatch.setattr(
        cli,
        "AsyncClientFactory",
        lambda: SimpleNamespace(invocation_scope=lambda cfg: nullcontext()),
    )
    monkeypatch.setattr(cli, "_get_client", lambda cfg, resource, factory: client)
    monkeypatch.setattr(sys, "argv", ["prog", *argv, "--page-size", "2", "--format", "json"])

    assert await cli.main() == cli.EXIT_SUCCESS
    output = capsys.readouterr()
    assert json.loads(output.out) == [{"value": 1}]
    assert json.loads(output.err.splitlines()[-1]) == {
        "pages_fetched": 1,
        "total_items": 1,
        "next_page_token": "continue",
        "page_size": 2,
    }


@pytest.mark.asyncio
async def test_edits_history_decodes_typed_primary_key():
    key = {"employeeId": {"type": "stringValue", "value": "50030"}}
    args = cli.build_parser().parse_args(
        [
            "object-type", "get-edits-history", "palantir", "Employee",
            "--object-primary-key", json.dumps(key),
        ]
    )
    method = AsyncMock(return_value=SimpleNamespace(data=[], next_page_token=None))

    await cli._invoke("object_type", "get_edits_history", SimpleNamespace(get_edits_history=method), args, None)

    assert method.await_args.kwargs["object_primary_key"] == key


@pytest.mark.asyncio
async def test_edits_history_rejects_malformed_primary_key_json():
    args = cli.build_parser().parse_args(
        [
            "object-type", "get-edits-history", "palantir", "Employee",
            "--object-primary-key", "{bad-json",
        ]
    )
    method = AsyncMock()

    with pytest.raises(json.JSONDecodeError):
        await cli._invoke("object_type", "get_edits_history", SimpleNamespace(get_edits_history=method), args, None)
    method.assert_not_called()


@pytest.mark.asyncio
async def test_query_attribution_is_plain_string_and_parameters_remain_json():
    args = cli.build_parser().parse_args(
        [
            "query", "execute", "palantir", "getEmployeesInCity",
            "--parameters", '{"city":"New York"}',
            "--attribution", "my-app",
        ]
    )
    method = AsyncMock(return_value={"value": 1})

    await cli._invoke("query", "execute", SimpleNamespace(execute=method), args, None)

    assert method.await_args.kwargs["parameters"] == {"city": "New York"}
    assert method.await_args.kwargs["attribution"] == "my-app"


REQUIRED_KEYWORDS = {
    ("action", "apply"): ("parameters",),
    ("action", "apply_batch"): ("requests",),
    ("action", "apply_with_overrides"): ("overrides", "request"),
    ("action_type", "get_by_rid_batch"): ("requests",),
    ("object_type", "get_by_rid_batch"): ("requests",),
    ("ontology", "load_metadata"): (
        "action_types", "interface_types", "link_types", "object_types", "query_types"
    ),
    ("ontology_interface", "aggregate"): ("aggregation", "group_by"),
    ("ontology_interface", "search"): (
        "augmented_interface_property_types", "augmented_properties",
        "augmented_shared_property_types", "other_interface_types",
        "selected_interface_property_types", "selected_object_types",
        "selected_shared_property_types",
    ),
    ("ontology_object", "aggregate"): ("aggregation", "group_by"),
    ("ontology_object", "search"): ("select",),
    ("ontology_object_set", "aggregate"): (
        "aggregation", "group_by", "object_set"
    ),
    ("ontology_object_set", "create_temporary"): ("object_set",),
    ("ontology_object_set", "load"): ("object_set", "select"),
    ("ontology_object_set", "load_links"): ("links", "object_set"),
    ("ontology_object_set", "load_multiple_object_types"): (
        "object_set", "select"
    ),
    ("ontology_object_set", "load_objects_or_interfaces"): (
        "object_set", "select"
    ),
    ("ontology_transaction", "post_edits"): ("edits",),
    ("query", "execute"): ("parameters",),
}


@pytest.mark.parametrize("resource,operation", REQUIRED_KEYWORDS)
def test_sdk_required_keywords_are_parser_required(resource, operation):
    spec = cli._spec_for(resource, operation)
    required = REQUIRED_KEYWORDS[(resource, operation)]
    assert spec["required"] == set(required)
    argv = [resource.replace("_", "-"), operation.replace("_", "-")]
    argv.extend("example" for _ in spec["positional"])
    argv.extend(part for name in required for part in ("--" + name.replace("_", "-"), "{}"))

    parsed = cli.build_parser().parse_args(argv)
    assert all(getattr(parsed, name) == "{}" for name in required)

    for name in required:
        flag = "--" + name.replace("_", "-")
        index = argv.index(flag)
        missing = argv[:index] + argv[index + 2 :]
        with pytest.raises(SystemExit) as exc:
            cli.build_parser().parse_args(missing)
        assert exc.value.code == cli.EXIT_USER_INPUT


@pytest.mark.parametrize(
    "resource,operation,flags,expected",
    [
        ("action", "apply", ["--parameters", "{}"], {"parameters": {}}),
        ("action", "apply_batch", ["--requests", "[]"], {"requests": []}),
        ("query", "execute", ["--parameters", "{}"], {"parameters": {}}),
        (
            "ontology_interface", "search",
            [
                "--augmented-interface-property-types", "{}",
                "--augmented-properties", "{}",
                "--augmented-shared-property-types", "{}",
                "--other-interface-types", "[]",
                "--selected-interface-property-types", "[]",
                "--selected-object-types", "[]",
                "--selected-shared-property-types", "[]",
            ],
            {
                "augmented_interface_property_types": {},
                "augmented_properties": {},
                "augmented_shared_property_types": {},
                "other_interface_types": [],
                "selected_interface_property_types": [],
                "selected_object_types": [],
                "selected_shared_property_types": [],
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_required_json_inputs_reach_sdk_as_parsed_values(
    resource, operation, flags, expected
):
    spec = cli._spec_for(resource, operation)
    argv = [resource.replace("_", "-"), operation.replace("_", "-")]
    argv.extend("example" for _ in spec["positional"])
    argv.extend(flags)
    args = cli.build_parser().parse_args(argv)
    method = AsyncMock(return_value={"ok": True})

    await cli._invoke(
        resource,
        operation,
        SimpleNamespace(**{spec["method"]: method}),
        args,
        None,
    )

    method.assert_awaited_once()
    for name, value in expected.items():
        assert method.await_args.kwargs[name] == value


@pytest.mark.parametrize(
    "operation,extra",
    [
        (
            "load",
            {"totalCount": 4, "computeUsage": 1.25, "propertySecurities": [{"id": 1}]},
        ),
        (
            "load_multiple_object_types",
            {
                "totalCount": 4,
                "interfaceToObjectTypeMappings": {"Person": {"name": "fullName"}},
                "interfaceToObjectTypeMappingsV2": {"Person": {}},
                "computeUsage": 2.5,
                "propertySecurities": [{"id": 2}],
            },
        ),
        ("load_objects_or_interfaces", {"totalCount": 4, "transactionId": "tx-1"}),
    ],
)
@pytest.mark.asyncio
async def test_object_set_load_preserves_single_page_metadata(
    operation, extra, capsys
):
    page = {"data": [_Item(1)], "nextPageToken": "more", **extra}
    client, _ = _client("ontology_object_set", operation, [page])
    args = cli.build_parser().parse_args(
        [
            "ontology-object-set", operation.replace("_", "-"), "palantir",
            "--object-set", "{}", "--select", "[]",
        ]
    )
    helper = cli.PaginationHelper(page_size=2)

    result = await cli._invoke_paginated(
        "ontology_object_set", operation, client, args, None, helper, None
    )
    helper.emit_metadata()
    metadata = json.loads(capsys.readouterr().err.splitlines()[-1])

    assert cli._model_to_dict(result) == [{"value": 1}]
    assert metadata["next_page_token"] == "more"
    assert metadata["response_metadata"] == [extra]


@pytest.mark.asyncio
async def test_object_set_load_preserves_ordered_page_metadata(capsys):
    class SdkPage:
        def __init__(self, value, token, metadata):
            self.data = [_Item(value)]
            self.next_page_token = token
            self.metadata = metadata

        def to_dict(self):
            return {
                "data": [item.to_dict() for item in self.data],
                "nextPageToken": self.next_page_token,
                **self.metadata,
            }

    pages = [
        SdkPage(1, "second", {
            "totalCount": 2,
            "interfaceToObjectTypeMappings": {"Person": {"name": "firstName"}},
            "interfaceToObjectTypeMappingsV2": {"Person": {}},
            "computeUsage": 1.0,
        }),
        SdkPage(2, None, {
            "totalCount": 2,
            "interfaceToObjectTypeMappings": {"Person": {"name": "secondName"}},
            "interfaceToObjectTypeMappingsV2": {"Person": {}},
            "computeUsage": 2.0,
        }),
    ]
    client, method = _client(
        "ontology_object_set", "load_multiple_object_types", pages
    )
    args = cli.build_parser().parse_args(
        [
            "ontology-object-set", "load-multiple-object-types", "palantir",
            "--object-set", "{}", "--select", "[]",
        ]
    )
    helper = cli.PaginationHelper(page_size=1, batch_pages=2)

    result = await cli._invoke_paginated(
        "ontology_object_set", "load_multiple_object_types",
        client, args, None, helper, None,
    )
    helper.emit_metadata()
    metadata = json.loads(capsys.readouterr().err.splitlines()[-1])

    assert cli._model_to_dict(result) == [{"value": 1}, {"value": 2}]
    assert method.await_count == 2
    assert metadata["pages_fetched"] == 2
    assert "next_page_token" not in metadata
    assert metadata["response_metadata"] == [page.metadata for page in pages]
