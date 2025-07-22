import pytest
from unittest.mock import MagicMock, AsyncMock, Mock
from sqlalchemy.exc import IntegrityError
from mcpgateway.services.tool_service import ToolService, ToolError, ToolNotFoundError, ToolValidationError, ToolInvocationError
from mcpgateway.schemas import ToolCreate, ToolUpdate, ToolRead

import asyncio


@pytest.fixture
def tool_service():
    return ToolService()


@pytest.fixture
def test_db():
    return MagicMock()


@pytest.fixture
def mock_tool():
    tool = MagicMock()
    tool.id = 1
    tool.name = "test_tool"
    tool.url = "http://example.com/tool"
    tool.description = "desc"
    tool.integration_type = "MCP"
    tool.request_type = "SSE"
    tool.headers = {}
    tool.input_schema = None
    tool.annotations = None
    tool.jsonpath_filter = None
    tool.auth_type = None
    tool.auth_value = None
    tool.gateway_id = 1
    tool.enabled = True
    tool.reachable = True
    tool.metrics_summary = {}
    tool.execution_count = 0
    tool.gateway_slug = ""
    tool.original_name_slug = "test_tool"
    tool.updated_at = None
    return tool


@pytest.mark.asyncio
def test_register_tool_success(tool_service, test_db, mock_tool):
    test_db.execute.return_value.scalar_one_or_none.return_value = None
    test_db.add = Mock()
    test_db.commit = Mock()
    test_db.refresh = Mock()
    tool_service._notify_tool_added = AsyncMock()
    tool_service._convert_tool_to_read = Mock(return_value="tool_read")
    ToolRead.model_validate = Mock(return_value="tool_read")
    tool_create = ToolCreate(
        name="test_tool",
        url="http://example.com/tool",
        description="desc",
        integration_type="MCP",
        request_type="SSE",
    )
    result = asyncio.run(tool_service.register_tool(test_db, tool_create))
    assert result == "tool_read"
    test_db.add.assert_called()
    test_db.commit.assert_called()
    test_db.refresh.assert_called()


@pytest.mark.asyncio
def test_register_tool_integrity_error(tool_service, test_db):
    test_db.execute.return_value.scalar_one_or_none.return_value = None
    test_db.add = Mock()
    test_db.commit = Mock(side_effect=IntegrityError("statement", "params", "orig"))
    test_db.refresh = Mock()
    tool_service._notify_tool_added = AsyncMock()
    tool_create = ToolCreate(
        name="test_tool",
        url="http://example.com/tool",
        description="desc",
        integration_type="MCP",
        request_type="SSE",
    )
    with pytest.raises(IntegrityError):
        asyncio.run(tool_service.register_tool(test_db, tool_create))


@pytest.mark.asyncio
def test_register_tool_other_error(tool_service, test_db):
    test_db.execute.return_value.scalar_one_or_none.return_value = None
    test_db.add = Mock(side_effect=Exception("fail"))
    tool_create = ToolCreate(
        name="test_tool",
        url="http://example.com/tool",
        description="desc",
        integration_type="MCP",
        request_type="SSE",
    )
    with pytest.raises(ToolError):
        asyncio.run(tool_service.register_tool(test_db, tool_create))


def test_get_tool_success(tool_service, test_db, mock_tool):
    test_db.get.return_value = mock_tool
    tool_service._convert_tool_to_read = Mock(return_value="tool_read")
    result = asyncio.run(tool_service.get_tool(test_db, 1))
    assert result == "tool_read"


def test_get_tool_not_found(tool_service, test_db):
    test_db.get.return_value = None
    with pytest.raises(ToolNotFoundError):
        asyncio.run(tool_service.get_tool(test_db, 1))


@pytest.mark.asyncio
def test_update_tool_success(tool_service, test_db, mock_tool):
    test_db.get.return_value = mock_tool
    test_db.commit = Mock()
    test_db.refresh = Mock()
    tool_service._notify_tool_updated = AsyncMock()
    tool_service._convert_tool_to_read = Mock(return_value="tool_read")
    ToolRead.model_validate = Mock(return_value="tool_read")
    tool_update = ToolUpdate(name="updated_tool")
    result = asyncio.run(tool_service.update_tool(test_db, 1, tool_update))
    assert result == "tool_read"
    test_db.commit.assert_called()
    test_db.refresh.assert_called()


@pytest.mark.asyncio
def test_update_tool_integrity_error(tool_service, test_db, mock_tool):
    test_db.get.return_value = mock_tool
    test_db.commit = Mock(side_effect=IntegrityError("statement", "params", "orig"))
    test_db.refresh = Mock()
    tool_update = ToolUpdate(name="updated_tool")
    with pytest.raises(IntegrityError):
        asyncio.run(tool_service.update_tool(test_db, 1, tool_update))


@pytest.mark.asyncio
def test_update_tool_not_found(tool_service, test_db):
    test_db.get.return_value = None
    tool_update = ToolUpdate(name="updated_tool")
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(tool_service.update_tool(test_db, 1, tool_update))
    assert "Tool not found" in str(exc_info.value)


@pytest.mark.asyncio
def test_update_tool_other_error(tool_service, test_db, mock_tool):
    test_db.get.return_value = mock_tool
    test_db.commit = Mock(side_effect=Exception("fail"))
    test_db.refresh = Mock()
    tool_update = ToolUpdate(name="updated_tool")
    with pytest.raises(ToolError):
        asyncio.run(tool_service.update_tool(test_db, 1, tool_update))


@pytest.mark.asyncio
def test_delete_tool_success(tool_service, test_db, mock_tool):
    test_db.get.return_value = mock_tool
    test_db.delete = Mock()
    test_db.commit = Mock()
    tool_service._notify_tool_deleted = AsyncMock()
    result = asyncio.run(tool_service.delete_tool(test_db, 1))
    test_db.delete.assert_called()
    test_db.commit.assert_called()


@pytest.mark.asyncio
def test_delete_tool_not_found(tool_service, test_db):
    test_db.get.return_value = None
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(tool_service.delete_tool(test_db, 1))
    assert "Tool not found" in str(exc_info.value)


@pytest.mark.asyncio
def test_delete_tool_other_error(tool_service, test_db, mock_tool):
    test_db.get.return_value = mock_tool
    test_db.delete = Mock(side_effect=Exception("fail"))
    test_db.rollback = Mock()
    with pytest.raises(ToolError):
        asyncio.run(tool_service.delete_tool(test_db, 1))
    test_db.rollback.assert_called()


@pytest.mark.asyncio
def test_toggle_tool_status_success(tool_service, test_db, mock_tool):
    test_db.get.return_value = mock_tool
    test_db.commit = Mock()
    test_db.refresh = Mock()
    tool_service._notify_tool_activated = AsyncMock()
    tool_service._notify_tool_deactivated = AsyncMock()
    tool_service._convert_tool_to_read = Mock(return_value="tool_read")
    ToolRead.model_validate = Mock(return_value="tool_read")
    # Only call commit/refresh if status actually changes
    mock_tool.enabled = False
    mock_tool.reachable = False
    result = asyncio.run(tool_service.toggle_tool_status(test_db, 1, True, True))
    assert result == "tool_read"
    test_db.commit.assert_called()
    test_db.refresh.assert_called()


def test_toggle_tool_status_only_reachable(tool_service, test_db, mock_tool):
    test_db.get.return_value = mock_tool
    test_db.commit = Mock()
    test_db.refresh = Mock()
    tool_service._notify_tool_activated = AsyncMock()
    tool_service._notify_tool_deactivated = AsyncMock()
    tool_service._convert_tool_to_read = Mock(return_value="tool_read")
    ToolRead.model_validate = Mock(return_value="tool_read")
    mock_tool.enabled = True
    mock_tool.reachable = False
    result = asyncio.run(tool_service.toggle_tool_status(test_db, 1, True, True))
    assert result == "tool_read"
    test_db.commit.assert_called()
    test_db.refresh.assert_called()


def test_toggle_tool_status_only_enabled(tool_service, test_db, mock_tool):
    test_db.get.return_value = mock_tool
    test_db.commit = Mock()
    test_db.refresh = Mock()
    tool_service._notify_tool_activated = AsyncMock()
    tool_service._notify_tool_deactivated = AsyncMock()
    tool_service._convert_tool_to_read = Mock(return_value="tool_read")
    ToolRead.model_validate = Mock(return_value="tool_read")
    mock_tool.enabled = False
    mock_tool.reachable = True
    result = asyncio.run(tool_service.toggle_tool_status(test_db, 1, True, True))
    assert result == "tool_read"
    test_db.commit.assert_called()
    test_db.refresh.assert_called()


@pytest.mark.asyncio
def test_list_tools(tool_service, test_db, mock_tool):
    test_db.execute.return_value.scalars.return_value.all.return_value = [mock_tool]
    tool_service._convert_tool_to_read = Mock(return_value="tool_read")
    result = asyncio.run(tool_service.list_tools(test_db))
    assert result == ["tool_read"]


@pytest.mark.asyncio
def test_list_server_tools(tool_service, test_db, mock_tool):
    test_db.execute.return_value.scalars.return_value.all.return_value = [mock_tool]
    tool_service._convert_tool_to_read = Mock(return_value="tool_read")
    result = asyncio.run(tool_service.list_server_tools(test_db, "server1"))
    assert result == ["tool_read"]


@pytest.mark.asyncio
def test_invoke_tool_not_found(tool_service, test_db):
    test_db.execute.return_value.scalar_one_or_none.return_value = None
    with pytest.raises(ToolNotFoundError):
        asyncio.run(tool_service.invoke_tool(test_db, "missing_tool", {}))


@pytest.mark.asyncio
def test_invoke_tool_inactive(tool_service, test_db, mock_tool):
    # First call returns None (active), second returns mock_tool (inactive)
    test_db.execute.return_value.scalar_one_or_none.side_effect = [None, mock_tool]
    mock_tool.enabled = False
    with pytest.raises(ToolNotFoundError):
        asyncio.run(tool_service.invoke_tool(test_db, "inactive_tool", {}))


@pytest.mark.asyncio
def test_invoke_tool_offline(tool_service, test_db, mock_tool):
    test_db.execute.return_value.scalar_one_or_none.return_value = mock_tool
    mock_tool.reachable = False
    with pytest.raises(ToolNotFoundError):
        asyncio.run(tool_service.invoke_tool(test_db, "offline_tool", {}))


@pytest.mark.asyncio
def test_invoke_tool_other_error(tool_service, test_db, mock_tool):
    test_db.execute.return_value.scalar_one_or_none.return_value = mock_tool
    mock_tool.reachable = True
    tool_service._http_client = MagicMock()
    tool_service._http_client.get = AsyncMock(side_effect=Exception("fail"))
    mock_tool.integration_type = "REST"
    mock_tool.request_type = "GET"
    mock_tool.url = "http://example.com/tool"
    mock_tool.headers = {}
    mock_tool.auth_value = None
    mock_tool.jsonpath_filter = None
    with pytest.raises(ToolInvocationError):
        asyncio.run(tool_service.invoke_tool(test_db, "tool_name", {}))


@pytest.mark.asyncio
def test_validate_tool_url_success(tool_service):
    tool_service._http_client = MagicMock()
    tool_service._http_client.get = AsyncMock(return_value=MagicMock(status_code=200, raise_for_status=Mock()))
    asyncio.run(tool_service._validate_tool_url("http://example.com/tool"))


@pytest.mark.asyncio
def test_validate_tool_url_fail(tool_service):
    tool_service._http_client = MagicMock()
    tool_service._http_client.get = AsyncMock(side_effect=Exception("fail"))
    with pytest.raises(ToolValidationError):
        asyncio.run(tool_service._validate_tool_url("http://example.com/tool"))


@pytest.mark.asyncio
def test_check_tool_health_true(tool_service, mock_tool):
    tool_service._http_client = MagicMock()
    tool_service._http_client.get = AsyncMock(return_value=MagicMock(is_success=True))
    assert asyncio.run(tool_service._check_tool_health(mock_tool)) is True


@pytest.mark.asyncio
def test_check_tool_health_false(tool_service, mock_tool):
    tool_service._http_client = MagicMock()
    tool_service._http_client.get = AsyncMock(side_effect=Exception("fail"))
    assert asyncio.run(tool_service._check_tool_health(mock_tool)) is False


@pytest.mark.asyncio
def test_aggregate_metrics(tool_service, test_db):
    test_db.execute.return_value.scalar.return_value = 0
    result = asyncio.run(tool_service.aggregate_metrics(test_db))
    assert isinstance(result, dict)


@pytest.mark.asyncio
def test_aggregate_metrics_nonzero(tool_service, test_db):
    test_db.execute.return_value.scalar.return_value = 5
    result = asyncio.run(tool_service.aggregate_metrics(test_db))
    assert isinstance(result, dict)
    assert result["total_executions"] == 5


@pytest.mark.asyncio
def test_reset_metrics_all(tool_service, test_db):
    test_db.execute = Mock()
    test_db.commit = Mock()
    asyncio.run(tool_service.reset_metrics(test_db))
    test_db.execute.assert_called()
    test_db.commit.assert_called()


@pytest.mark.asyncio
def test_reset_metrics_tool_id(tool_service, test_db):
    test_db.execute = Mock()
    test_db.commit = Mock()
    asyncio.run(tool_service.reset_metrics(test_db, tool_id=1))
    test_db.execute.assert_called()
    test_db.commit.assert_called()


# --- Additional tests for coverage ---
import types
import datetime


def test_convert_tool_to_read_basic(tool_service, mock_tool):
    # Should not raise and return a ToolRead (mocked)
    ToolRead.model_validate = Mock(return_value="tool_read")
    result = tool_service._convert_tool_to_read(mock_tool)
    assert result == "tool_read"


@pytest.mark.asyncio
async def test_notify_tool_added(tool_service, mock_tool):
    # Should call _publish_event
    tool_service._publish_event = AsyncMock()
    await tool_service._notify_tool_added(mock_tool)
    tool_service._publish_event.assert_called()


@pytest.mark.asyncio
async def test_notify_tool_updated(tool_service, mock_tool):
    tool_service._publish_event = AsyncMock()
    await tool_service._notify_tool_updated(mock_tool)
    tool_service._publish_event.assert_called()


@pytest.mark.asyncio
async def test_notify_tool_activated(tool_service, mock_tool):
    tool_service._publish_event = AsyncMock()
    await tool_service._notify_tool_activated(mock_tool)
    tool_service._publish_event.assert_called()


@pytest.mark.asyncio
async def test_notify_tool_deactivated(tool_service, mock_tool):
    tool_service._publish_event = AsyncMock()
    await tool_service._notify_tool_deactivated(mock_tool)
    tool_service._publish_event.assert_called()


@pytest.mark.asyncio
async def test_notify_tool_deleted(tool_service, mock_tool):
    tool_service._publish_event = AsyncMock()
    await tool_service._notify_tool_deleted({"id": 1, "name": "test_tool"})
    tool_service._publish_event.assert_called()


@pytest.mark.asyncio
async def test_notify_tool_removed(tool_service, mock_tool):
    tool_service._publish_event = AsyncMock()
    await tool_service._notify_tool_removed(mock_tool)
    tool_service._publish_event.assert_called()


@pytest.mark.asyncio
async def test_publish_event(tool_service):
    # Should put event in all queues
    queue1 = asyncio.Queue()
    queue2 = asyncio.Queue()
    tool_service._event_subscribers = [queue1, queue2]
    event = {"type": "test", "data": {}}
    await tool_service._publish_event(event)
    assert await queue1.get() == event
    assert await queue2.get() == event


@pytest.mark.asyncio
async def test_subscribe_events_and_event_generator(tool_service):
    # Test subscribe_events yields events and cleans up
    async def put_event(q, event):
        await asyncio.sleep(0.01)
        await q.put(event)

    event = {"type": "test", "data": {}}
    gen = tool_service.subscribe_events()
    # Advance generator to ensure queue is created
    first = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)
    queue = tool_service._event_subscribers[-1]
    task = asyncio.create_task(put_event(queue, event))
    result = await first
    assert result == event
    task.cancel()
    # event_generator is similar
    gen2 = tool_service.event_generator()
    first2 = asyncio.create_task(gen2.__anext__())
    await asyncio.sleep(0)
    queue2 = tool_service._event_subscribers[-1]
    task2 = asyncio.create_task(put_event(queue2, event))
    result2 = await first2
    assert result2 == event
    task2.cancel()


def test_toolnameconflicterror_str():
    from mcpgateway.services.tool_service import ToolNameConflictError

    err = ToolNameConflictError("test_tool", enabled=False, tool_id=123)
    assert "currently inactive" in str(err)
    assert err.name == "test_tool"
    assert err.enabled is False
    assert err.tool_id == 123


def test_toolvalidationerror_str():
    from mcpgateway.services.tool_service import ToolValidationError

    err = ToolValidationError("Invalid tool configuration")
    assert "Invalid tool configuration" in str(err)


def test_toolinvocationerror_str():
    from mcpgateway.services.tool_service import ToolInvocationError

    err = ToolInvocationError("Failed to invoke tool")
    assert "Failed to invoke tool" in str(err)


def test_toolnotfounderror_str():
    from mcpgateway.services.tool_service import ToolNotFoundError

    err = ToolNotFoundError("Tool xyz not found")
    assert "Tool xyz not found" in str(err)


def test_toolerror_str():
    from mcpgateway.services.tool_service import ToolError

    err = ToolError("Something went wrong")
    assert "Something went wrong" in str(err)


def test_register_tool_generic_exception(tool_service, test_db):
    # Simulate generic exception in register_tool
    test_db.execute.return_value.scalar_one_or_none.return_value = None
    test_db.add = Mock()
    test_db.commit = Mock(side_effect=Exception("fail"))
    tool_create = ToolCreate(
        name="test_tool",
        url="http://example.com/tool",
        description="desc",
        integration_type="MCP",
        request_type="SSE",
    )
    with pytest.raises(ToolError):
        asyncio.run(tool_service.register_tool(test_db, tool_create))


def test_update_tool_generic_exception(tool_service, test_db, mock_tool):
    test_db.get.return_value = mock_tool
    test_db.commit = Mock()
    test_db.refresh = Mock(side_effect=Exception("fail"))
    tool_update = ToolUpdate(name="updated_tool")
    with pytest.raises(ToolError):
        asyncio.run(tool_service.update_tool(test_db, 1, tool_update))


def test_delete_tool_generic_exception(tool_service, test_db, mock_tool):
    test_db.get.return_value = mock_tool
    test_db.delete = Mock()
    test_db.commit = Mock(side_effect=Exception("fail"))
    test_db.rollback = Mock()
    with pytest.raises(ToolError):
        asyncio.run(tool_service.delete_tool(test_db, 1))
    test_db.rollback.assert_called()


def test_toggle_tool_status_no_change(tool_service, test_db, mock_tool):
    test_db.get.return_value = mock_tool
    # No change in enabled or reachable
    mock_tool.enabled = True
    mock_tool.reachable = True
    tool_service._convert_tool_to_read = Mock(return_value="tool_read")
    ToolRead.model_validate = Mock(return_value="tool_read")
    result = asyncio.run(tool_service.toggle_tool_status(test_db, 1, True, True))
    assert result == "tool_read"


def test_invoke_tool_invalid_type(tool_service, test_db, mock_tool):
    test_db.execute.return_value.scalar_one_or_none.return_value = mock_tool
    mock_tool.reachable = True
    mock_tool.integration_type = "INVALID"
    mock_tool.request_type = "GET"
    mock_tool.url = "http://example.com/tool"
    mock_tool.headers = {}
    mock_tool.auth_value = None
    mock_tool.jsonpath_filter = None
    result = asyncio.run(tool_service.invoke_tool(test_db, "tool_name", {}))
    assert result.content[0].text == "Invalid tool type"


def test_invoke_tool_missing_url_param(tool_service, test_db, mock_tool):
    test_db.execute.return_value.scalar_one_or_none.return_value = mock_tool
    mock_tool.reachable = True
    mock_tool.integration_type = "REST"
    mock_tool.request_type = "GET"
    mock_tool.url = "http://example.com/tool/{foo}"
    mock_tool.headers = {}
    mock_tool.auth_value = None
    mock_tool.jsonpath_filter = None
    with pytest.raises(ToolInvocationError):
        asyncio.run(tool_service.invoke_tool(test_db, "tool_name", {}))


def test_record_tool_metric(tool_service, test_db, mock_tool):
    # Should call db.add and db.commit
    test_db.add = Mock()
    test_db.commit = Mock()
    import time

    start_time = time.monotonic()
    asyncio.run(tool_service._record_tool_metric(test_db, mock_tool, start_time, True, None))
    test_db.add.assert_called()
    test_db.commit.assert_called()


def test_reset_metrics_with_and_without_tool_id(tool_service, test_db):
    test_db.execute = Mock()
    test_db.commit = Mock()
    # With tool_id
    asyncio.run(tool_service.reset_metrics(test_db, tool_id=1))
    test_db.execute.assert_called()
    test_db.commit.assert_called()
    # Without tool_id
    asyncio.run(tool_service.reset_metrics(test_db))
    test_db.execute.assert_called()
    test_db.commit.assert_called()


def test_list_tools_include_inactive(tool_service, test_db, mock_tool):
    test_db.execute.return_value.scalars.return_value.all.return_value = [mock_tool]
    tool_service._convert_tool_to_read = Mock(return_value="tool_read")
    result = asyncio.run(tool_service.list_tools(test_db, include_inactive=True))
    assert result == ["tool_read"]


def test_list_server_tools_include_inactive(tool_service, test_db, mock_tool):
    test_db.execute.return_value.scalars.return_value.all.return_value = [mock_tool]
    tool_service._convert_tool_to_read = Mock(return_value="tool_read")
    result = asyncio.run(tool_service.list_server_tools(test_db, "server1", include_inactive=True))
    assert result == ["tool_read"]


def test_convert_tool_to_read_auth_basic(tool_service, mock_tool):
    mock_tool.auth_type = "basic"
    mock_tool.auth_value = "irrelevant"
    ToolRead.model_validate = Mock(return_value="tool_read")
    from unittest.mock import patch

    with patch("mcpgateway.services.tool_service.decode_auth", return_value={"Authorization": "Basic dXNlcjpwYXNz"}):
        result = tool_service._convert_tool_to_read(mock_tool)
    assert result == "tool_read"


def test_convert_tool_to_read_auth_bearer(tool_service, mock_tool):
    mock_tool.auth_type = "bearer"
    mock_tool.auth_value = "irrelevant"
    ToolRead.model_validate = Mock(return_value="tool_read")
    from unittest.mock import patch

    with patch("mcpgateway.services.tool_service.decode_auth", return_value={"Authorization": "Bearer sometoken"}):
        result = tool_service._convert_tool_to_read(mock_tool)
    assert result == "tool_read"


def test_convert_tool_to_read_auth_headers(tool_service, mock_tool):
    mock_tool.auth_type = "authheaders"
    mock_tool.auth_value = "irrelevant"
    ToolRead.model_validate = Mock(return_value="tool_read")
    from unittest.mock import patch

    with patch("mcpgateway.services.tool_service.decode_auth", return_value={"X-Api-Key": "secret"}):
        result = tool_service._convert_tool_to_read(mock_tool)
    assert result == "tool_read"


def test_invoke_tool_rest_get(tool_service, test_db, mock_tool):
    test_db.execute.return_value.scalar_one_or_none.return_value = mock_tool
    mock_tool.reachable = True
    mock_tool.integration_type = "REST"
    mock_tool.request_type = "GET"
    mock_tool.url = "http://example.com/tool"
    mock_tool.headers = {}
    mock_tool.auth_value = None
    mock_tool.jsonpath_filter = None
    tool_service._http_client = MagicMock()
    tool_service._http_client.get = AsyncMock(return_value=MagicMock(status_code=200, json=Mock(return_value={}), raise_for_status=Mock()))
    from unittest.mock import patch

    with patch("mcpgateway.services.tool_service.extract_using_jq", return_value={}):
        result = asyncio.run(tool_service.invoke_tool(test_db, "tool_name", {}))
    assert result.content[0].text == "{}"


def test_invoke_tool_rest_post(tool_service, test_db, mock_tool):
    test_db.execute.return_value.scalar_one_or_none.return_value = mock_tool
    mock_tool.reachable = True
    mock_tool.integration_type = "REST"
    mock_tool.request_type = "POST"
    mock_tool.url = "http://example.com/tool"
    mock_tool.headers = {}
    mock_tool.auth_value = None
    mock_tool.jsonpath_filter = None
    tool_service._http_client = MagicMock()
    tool_service._http_client.request = AsyncMock(return_value=MagicMock(status_code=200, json=Mock(return_value={}), raise_for_status=Mock()))
    from unittest.mock import patch

    with patch("mcpgateway.services.tool_service.extract_using_jq", return_value={}):
        result = asyncio.run(tool_service.invoke_tool(test_db, "tool_name", {}))
    assert result.content[0].text == "{}"


def test_invoke_tool_rest_204(tool_service, test_db, mock_tool):
    test_db.execute.return_value.scalar_one_or_none.return_value = mock_tool
    mock_tool.reachable = True
    mock_tool.integration_type = "REST"
    mock_tool.request_type = "POST"
    mock_tool.url = "http://example.com/tool"
    mock_tool.headers = {}
    mock_tool.auth_value = None
    mock_tool.jsonpath_filter = None
    tool_service._http_client = MagicMock()
    tool_service._http_client.request = AsyncMock(return_value=MagicMock(status_code=204, raise_for_status=Mock()))
    from unittest.mock import patch

    with patch("mcpgateway.services.tool_service.extract_using_jq", return_value={}):
        result = asyncio.run(tool_service.invoke_tool(test_db, "tool_name", {}))
    assert result.content[0].text.startswith("Request completed successfully")


def test_invoke_tool_rest_error_status(tool_service, test_db, mock_tool):
    test_db.execute.return_value.scalar_one_or_none.return_value = mock_tool
    mock_tool.reachable = True
    mock_tool.integration_type = "REST"
    mock_tool.request_type = "POST"
    mock_tool.url = "http://example.com/tool"
    mock_tool.headers = {}
    mock_tool.auth_value = None
    mock_tool.jsonpath_filter = None
    tool_service._http_client = MagicMock()
    tool_service._http_client.request = AsyncMock(return_value=MagicMock(status_code=500, json=Mock(return_value={"error": "fail"}), raise_for_status=Mock()))
    from unittest.mock import patch

    with patch("mcpgateway.services.tool_service.extract_using_jq", return_value={}):
        result = asyncio.run(tool_service.invoke_tool(test_db, "tool_name", {}))
    assert result.is_error


def test_invoke_tool_exception_branch(tool_service, test_db, mock_tool):
    test_db.execute.return_value.scalar_one_or_none.return_value = mock_tool
    mock_tool.reachable = True
    mock_tool.integration_type = "REST"
    mock_tool.request_type = "GET"
    mock_tool.url = "http://example.com/tool"
    mock_tool.headers = {}
    mock_tool.auth_value = None
    mock_tool.jsonpath_filter = None
    tool_service._http_client = MagicMock()
    tool_service._http_client.get = AsyncMock(side_effect=Exception("fail"))
    with pytest.raises(ToolInvocationError):
        asyncio.run(tool_service.invoke_tool(test_db, "tool_name", {}))


def test_event_generator_cleanup(tool_service):
    # Test generator exit and cleanup
    import asyncio

    async def run_gen():
        gen = tool_service.event_generator()
        task = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Should remove the queue
        assert len(tool_service._event_subscribers) == 0

    asyncio.run(run_gen())
