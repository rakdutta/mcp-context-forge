# -*- coding: utf-8 -*-
"""Tool Service Implementation.

Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Mihai Criveti

This module implements tool management and invocation according to the MCP specification.
It handles:
- Tool registration and validation
- Tool invocation with schema validation
- Tool federation across gateways
- Event notifications for tool changes
- Active/inactive tool management
"""

# Standard
import asyncio
import base64
from datetime import datetime, timezone
import json
import logging
import re
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

# Third-Party
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy import delete, func, not_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import Gateway as DbGateway
from mcpgateway.db import server_tool_association
from mcpgateway.db import Tool as DbTool
from mcpgateway.db import ToolMetric
from mcpgateway.models import TextContent, ToolResult
from mcpgateway.schemas import (
    ToolCreate,
    ToolRead,
    ToolUpdate,
)
from mcpgateway.utils.create_slug import slugify
from mcpgateway.utils.retry_manager import ResilientHttpClient
from mcpgateway.utils.services_auth import decode_auth

# Local
from ..config import extract_using_jq

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """Base class for tool-related errors.

    Examples:
        >>> from mcpgateway.services.tool_service import ToolError
        >>> err = ToolError("Something went wrong")
        >>> str(err)
        'Something went wrong'
    """


class ToolNotFoundError(ToolError):
    """Raised when a requested tool is not found.

    Examples:
        >>> from mcpgateway.services.tool_service import ToolNotFoundError
        >>> err = ToolNotFoundError("Tool xyz not found")
        >>> str(err)
        'Tool xyz not found'
        >>> isinstance(err, ToolError)
        True
    """


class ToolNameConflictError(ToolError):
    """Raised when a tool name conflicts with existing (active or inactive) tool."""

    def __init__(self, name: str, enabled: bool = True, tool_id: Optional[int] = None):
        """Initialize the error with tool information.

        Args:
            name: The conflicting tool name.
            enabled: Whether the existing tool is enabled or not.
            tool_id: ID of the existing tool if available.

        Examples:
            >>> from mcpgateway.services.tool_service import ToolNameConflictError
            >>> err = ToolNameConflictError('test_tool', enabled=False, tool_id=123)
            >>> str(err)
            'Tool already exists with name: test_tool (currently inactive, ID: 123)'
            >>> err.name
            'test_tool'
            >>> err.enabled
            False
            >>> err.tool_id
            123
        """
        self.name = name
        self.enabled = enabled
        self.tool_id = tool_id
        message = f"Tool already exists with name: {name}"
        if not enabled:
            message += f" (currently inactive, ID: {tool_id})"
        super().__init__(message)


class ToolValidationError(ToolError):
    """Raised when tool validation fails.

    Examples:
        >>> from mcpgateway.services.tool_service import ToolValidationError
        >>> err = ToolValidationError("Invalid tool configuration")
        >>> str(err)
        'Invalid tool configuration'
        >>> isinstance(err, ToolError)
        True
    """


class ToolInvocationError(ToolError):
    """Raised when tool invocation fails.

    Examples:
        >>> from mcpgateway.services.tool_service import ToolInvocationError
        >>> err = ToolInvocationError("Failed to invoke tool")
        >>> str(err)
        'Failed to invoke tool'
        >>> isinstance(err, ToolError)
        True
    """


class ToolService:
    """Service for managing and invoking tools.

    Handles:
    - Tool registration and deregistration.
    - Tool invocation and validation.
    - Tool federation.
    - Event notifications.
    - Active/inactive tool management.
    """

    def __init__(self):
        """Initialize the tool service.

        Examples:
            >>> from mcpgateway.services.tool_service import ToolService
            >>> service = ToolService()
            >>> isinstance(service._event_subscribers, list)
            True
            >>> len(service._event_subscribers)
            0
            >>> hasattr(service, '_http_client')
            True
        """
        self._event_subscribers: List[asyncio.Queue] = []
        self._http_client = ResilientHttpClient(client_args={"timeout": settings.federation_timeout, "verify": not settings.skip_ssl_verify})

    async def initialize(self) -> None:
        """Initialize the service.

        Examples:
            >>> from mcpgateway.services.tool_service import ToolService
            >>> service = ToolService()
            >>> import asyncio
            >>> asyncio.run(service.initialize())  # Should log "Initializing tool service"
        """
        logger.info("Initializing tool service")

    async def shutdown(self) -> None:
        """Shutdown the service.

        Examples:
            >>> from mcpgateway.services.tool_service import ToolService
            >>> service = ToolService()
            >>> import asyncio
            >>> asyncio.run(service.shutdown())  # Should log "Tool service shutdown complete"
        """
        await self._http_client.aclose()
        logger.info("Tool service shutdown complete")

    def _convert_tool_to_read(self, tool: DbTool) -> ToolRead:
        """Converts a DbTool instance into a ToolRead model, including aggregated metrics and
        new API gateway fields: request_type and authentication credentials (masked).

        Args:
            tool (DbTool): The ORM instance of the tool.

        Returns:
            ToolRead: The Pydantic model representing the tool, including aggregated metrics and new fields.
        """
        tool_dict = tool.__dict__.copy()
        tool_dict.pop("_sa_instance_state", None)
        tool_dict["execution_count"] = tool.execution_count
        tool_dict["metrics"] = tool.metrics_summary
        tool_dict["request_type"] = tool.request_type
        tool_dict["annotations"] = tool.annotations or {}

        decoded_auth_value = decode_auth(tool.auth_value)
        if tool.auth_type == "basic":
            decoded_bytes = base64.b64decode(decoded_auth_value["Authorization"].split("Basic ")[1])
            username, password = decoded_bytes.decode("utf-8").split(":")
            tool_dict["auth"] = {
                "auth_type": "basic",
                "username": username,
                "password": "********" if password else None,
            }
        elif tool.auth_type == "bearer":
            tool_dict["auth"] = {
                "auth_type": "bearer",
                "token": "********" if decoded_auth_value["Authorization"] else None,
            }
        elif tool.auth_type == "authheaders":
            tool_dict["auth"] = {
                "auth_type": "authheaders",
                "auth_header_key": next(iter(decoded_auth_value)),
                "auth_header_value": "********" if decoded_auth_value[next(iter(decoded_auth_value))] else None,
            }
        else:
            tool_dict["auth"] = None

        tool_dict["name"] = tool.name
        tool_dict["gateway_slug"] = tool.gateway_slug if tool.gateway_slug else ""
        tool_dict["original_name_slug"] = tool.original_name_slug

        return ToolRead.model_validate(tool_dict)

    async def _record_tool_metric(self, db: Session, tool: DbTool, start_time: float, success: bool, error_message: Optional[str]) -> None:
        """
        Records a metric for a tool invocation.

        This function calculates the response time using the provided start time and records
        the metric details (including whether the invocation was successful and any error message)
        into the database. The metric is then committed to the database.

        Args:
            db (Session): The SQLAlchemy database session.
            tool (DbTool): The tool that was invoked.
            start_time (float): The monotonic start time of the invocation.
            success (bool): True if the invocation succeeded; otherwise, False.
            error_message (Optional[str]): The error message if the invocation failed, otherwise None.
        """
        end_time = time.monotonic()
        response_time = end_time - start_time
        metric = ToolMetric(
            tool_id=tool.id,
            response_time=response_time,
            is_success=success,
            error_message=error_message,
        )
        db.add(metric)
        db.commit()

    async def register_tool(self, db: Session, tool: ToolCreate) -> ToolRead:
        """Register a new tool.

        Args:
            db: Database session.
            tool: Tool creation schema.

        Returns:
            Created tool information.

        Raises:
            IntegrityError: If there is a database integrity error.
            ToolError: For other tool registration errors.

        Examples:
            >>> from mcpgateway.services.tool_service import ToolService
            >>> from unittest.mock import MagicMock, AsyncMock
            >>> from mcpgateway.schemas import ToolRead
            >>> service = ToolService()
            >>> db = MagicMock()
            >>> tool = MagicMock()
            >>> tool.name = 'test'
            >>> db.execute.return_value.scalar_one_or_none.return_value = None
            >>> mock_gateway = MagicMock()
            >>> mock_gateway.name = 'test_gateway'
            >>> db.add = MagicMock()
            >>> db.commit = MagicMock()
            >>> def mock_refresh(obj):
            ...     obj.gateway = mock_gateway
            >>> db.refresh = MagicMock(side_effect=mock_refresh)
            >>> service._notify_tool_added = AsyncMock()
            >>> service._convert_tool_to_read = MagicMock(return_value='tool_read')
            >>> ToolRead.model_validate = MagicMock(return_value='tool_read')
            >>> import asyncio
            >>> asyncio.run(service.register_tool(db, tool))
            'tool_read'
        """
        try:
            if tool.auth is None:
                auth_type = None
                auth_value = None
            else:
                auth_type = tool.auth.auth_type
                auth_value = tool.auth.auth_value

            db_tool = DbTool(
                original_name=tool.name,
                original_name_slug=slugify(tool.name),
                url=str(tool.url),
                description=tool.description,
                integration_type=tool.integration_type,
                request_type=tool.request_type,
                headers=tool.headers,
                input_schema=tool.input_schema,
                annotations=tool.annotations,
                jsonpath_filter=tool.jsonpath_filter,
                auth_type=auth_type,
                auth_value=auth_value,
                gateway_id=tool.gateway_id,
            )
            db.add(db_tool)
            db.commit()
            db.refresh(db_tool)
            await self._notify_tool_added(db_tool)
            logger.info(f"Registered tool: {db_tool.name}")
            return self._convert_tool_to_read(db_tool)
        except IntegrityError as ie:
            logger.error(f"IntegrityError during tool registration: {ie}")
            raise ie
        except Exception as ex:
            raise ToolError(f"Failed to register tool: {str(ex)}")

    async def list_tools(self, db: Session, include_inactive: bool = False, cursor: Optional[str] = None) -> List[ToolRead]:
        """
        Retrieve a list of registered tools from the database.

        Args:
            db (Session): The SQLAlchemy database session.
            include_inactive (bool): If True, include inactive tools in the result.
                Defaults to False.
            cursor (Optional[str], optional): An opaque cursor token for pagination. Currently,
                this parameter is ignored. Defaults to None.

        Returns:
            List[ToolRead]: A list of registered tools represented as ToolRead objects.

        Examples:
            >>> from mcpgateway.services.tool_service import ToolService
            >>> from unittest.mock import MagicMock
            >>> service = ToolService()
            >>> db = MagicMock()
            >>> tool_read = MagicMock()
            >>> service._convert_tool_to_read = MagicMock(return_value=tool_read)
            >>> db.execute.return_value.scalars.return_value.all.return_value = [MagicMock()]
            >>> import asyncio
            >>> result = asyncio.run(service.list_tools(db))
            >>> isinstance(result, list)
            True
        """
        query = select(DbTool)
        cursor = None  # Placeholder for pagination; ignore for now
        logger.debug(f"Listing tools with include_inactive={include_inactive}, cursor={cursor}")
        if not include_inactive:
            query = query.where(DbTool.enabled)
        tools = db.execute(query).scalars().all()
        return [self._convert_tool_to_read(t) for t in tools]

    async def list_server_tools(self, db: Session, server_id: str, include_inactive: bool = False, cursor: Optional[str] = None) -> List[ToolRead]:
        """
        Retrieve a list of registered tools from the database.

        Args:
            db (Session): The SQLAlchemy database session.
            server_id (str): Server ID
            include_inactive (bool): If True, include inactive tools in the result.
                Defaults to False.
            cursor (Optional[str], optional): An opaque cursor token for pagination. Currently,
                this parameter is ignored. Defaults to None.

        Returns:
            List[ToolRead]: A list of registered tools represented as ToolRead objects.

        Examples:
            >>> from mcpgateway.services.tool_service import ToolService
            >>> from unittest.mock import MagicMock
            >>> service = ToolService()
            >>> db = MagicMock()
            >>> tool_read = MagicMock()
            >>> service._convert_tool_to_read = MagicMock(return_value=tool_read)
            >>> db.execute.return_value.scalars.return_value.all.return_value = [MagicMock()]
            >>> import asyncio
            >>> result = asyncio.run(service.list_server_tools(db, 'server1'))
            >>> isinstance(result, list)
            True
        """
        query = select(DbTool).join(server_tool_association, DbTool.id == server_tool_association.c.tool_id).where(server_tool_association.c.server_id == server_id)
        cursor = None  # Placeholder for pagination; ignore for now
        logger.debug(f"Listing server tools for server_id={server_id} with include_inactive={include_inactive}, cursor={cursor}")
        if not include_inactive:
            query = query.where(DbTool.enabled)
        tools = db.execute(query).scalars().all()
        return [self._convert_tool_to_read(t) for t in tools]

    async def get_tool(self, db: Session, tool_id: str) -> ToolRead:
        """
        Retrieve a tool by its ID.

        Args:
            db (Session): The SQLAlchemy database session.
            tool_id (str): The unique identifier of the tool.

        Returns:
            ToolRead: The tool object.

        Raises:
            ToolNotFoundError: If the tool is not found.

        Examples:
            >>> from mcpgateway.services.tool_service import ToolService
            >>> from unittest.mock import MagicMock
            >>> service = ToolService()
            >>> db = MagicMock()
            >>> tool = MagicMock()
            >>> db.get.return_value = tool
            >>> service._convert_tool_to_read = MagicMock(return_value='tool_read')
            >>> import asyncio
            >>> asyncio.run(service.get_tool(db, 'tool_id'))
            'tool_read'
        """
        tool = db.get(DbTool, tool_id)
        if not tool:
            raise ToolNotFoundError(f"Tool not found: {tool_id}")
        return self._convert_tool_to_read(tool)

    async def delete_tool(self, db: Session, tool_id: str) -> None:
        """
        Delete a tool by its ID.

        Args:
            db (Session): The SQLAlchemy database session.
            tool_id (str): The unique identifier of the tool.

        Raises:
            ToolNotFoundError: If the tool is not found.
            ToolError: For other deletion errors.

        Examples:
            >>> from mcpgateway.services.tool_service import ToolService
            >>> from unittest.mock import MagicMock, AsyncMock
            >>> service = ToolService()
            >>> db = MagicMock()
            >>> tool = MagicMock()
            >>> db.get.return_value = tool
            >>> db.delete = MagicMock()
            >>> db.commit = MagicMock()
            >>> service._notify_tool_deleted = AsyncMock()
            >>> import asyncio
            >>> asyncio.run(service.delete_tool(db, 'tool_id'))
        """
        try:
            tool = db.get(DbTool, tool_id)
            if not tool:
                raise ToolNotFoundError(f"Tool not found: {tool_id}")
            tool_info = {"id": tool.id, "name": tool.name}
            db.delete(tool)
            db.commit()
            await self._notify_tool_deleted(tool_info)
            logger.info(f"Permanently deleted tool: {tool_info['name']}")
        except Exception as e:
            db.rollback()
            raise ToolError(f"Failed to delete tool: {str(e)}")

    async def toggle_tool_status(self, db: Session, tool_id: str, activate: bool, reachable: bool) -> ToolRead:
        """
        Toggle the activation status of a tool.

        Args:
            db (Session): The SQLAlchemy database session.
            tool_id (str): The unique identifier of the tool.
            activate (bool): True to activate, False to deactivate.
            reachable (bool): True if the tool is reachable.

        Returns:
            ToolRead: The updated tool object.

        Raises:
            ToolNotFoundError: If the tool is not found.
            ToolError: For other errors.

        Examples:
            >>> from mcpgateway.services.tool_service import ToolService
            >>> from unittest.mock import MagicMock, AsyncMock
            >>> from mcpgateway.schemas import ToolRead
            >>> service = ToolService()
            >>> db = MagicMock()
            >>> tool = MagicMock()
            >>> db.get.return_value = tool
            >>> db.commit = MagicMock()
            >>> db.refresh = MagicMock()
            >>> service._notify_tool_activated = AsyncMock()
            >>> service._notify_tool_deactivated = AsyncMock()
            >>> service._convert_tool_to_read = MagicMock(return_value='tool_read')
            >>> ToolRead.model_validate = MagicMock(return_value='tool_read')
            >>> import asyncio
            >>> asyncio.run(service.toggle_tool_status(db, 'tool_id', True, True))
            'tool_read'
        """
        try:
            tool = db.get(DbTool, tool_id)
            if not tool:
                raise ToolNotFoundError(f"Tool not found: {tool_id}")

            is_activated = is_reachable = False
            if tool.enabled != activate:
                tool.enabled = activate
                is_activated = True

            if tool.reachable != reachable:
                tool.reachable = reachable
                is_reachable = True

            if is_activated or is_reachable:
                tool.updated_at = datetime.now(timezone.utc)

                db.commit()
                db.refresh(tool)
                if activate:
                    await self._notify_tool_activated(tool)
                else:
                    await self._notify_tool_deactivated(tool)
                logger.info(f"Tool: {tool.name} is {'enabled' if activate else 'disabled'}{' and accessible' if reachable else ' but inaccessible'}")

            return self._convert_tool_to_read(tool)
        except Exception as e:
            db.rollback()
            raise ToolError(f"Failed to toggle tool status: {str(e)}")

    async def invoke_tool(self, db: Session, name: str, arguments: Dict[str, Any]) -> ToolResult:
        """
        Invoke a registered tool and record execution metrics.

        Args:
            db: Database session.
            name: Name of tool to invoke.
            arguments: Tool arguments.

        Returns:
            Tool invocation result.

        Raises:
            ToolNotFoundError: If tool not found.
            ToolInvocationError: If invocation fails.

        Examples:
            >>> from mcpgateway.services.tool_service import ToolService
            >>> from unittest.mock import MagicMock
            >>> service = ToolService()
            >>> db = MagicMock()
            >>> tool = MagicMock()
            >>> db.execute.return_value.scalar_one_or_none.side_effect = [tool, None]
            >>> tool.reachable = True
            >>> import asyncio
            >>> result = asyncio.run(service.invoke_tool(db, 'tool_name', {}))
            >>> isinstance(result, object)
            True
        """
        # pylint: disable=comparison-with-callable
        tool = db.execute(select(DbTool).where(DbTool.name == name).where(DbTool.enabled)).scalar_one_or_none()
        if not tool:
            inactive_tool = db.execute(select(DbTool).where(DbTool.name == name).where(not_(DbTool.enabled))).scalar_one_or_none()
            if inactive_tool:
                raise ToolNotFoundError(f"Tool '{name}' exists but is inactive")
            raise ToolNotFoundError(f"Tool not found: {name}")

        # is_reachable = db.execute(select(DbTool.reachable).where(slug_expr == name)).scalar_one_or_none()
        is_reachable = tool.reachable

        if not is_reachable:
            raise ToolNotFoundError(f"Tool '{name}' exists but is currently offline. Please verify if it is running.")

        start_time = time.monotonic()
        success = False
        error_message = None
        try:
            # tool.validate_arguments(arguments)
            # Build headers with auth if necessary.
            headers = tool.headers or {}
            if tool.integration_type == "REST":
                credentials = decode_auth(tool.auth_value)
                headers.update(credentials)

                # Build the payload based on integration type.
                payload = arguments.copy()

                # Handle URL path parameter substitution
                final_url = tool.url
                if "{" in tool.url and "}" in tool.url:
                    # Extract path parameters from URL template and arguments
                    url_params = re.findall(r"\{(\w+)\}", tool.url)
                    url_substitutions = {}

                    for param in url_params:
                        if param in payload:
                            url_substitutions[param] = payload.pop(param)  # Remove from payload
                            final_url = final_url.replace(f"{{{param}}}", str(url_substitutions[param]))
                        else:
                            raise ToolInvocationError(f"Required URL parameter '{param}' not found in arguments")

                # Use the tool's request_type rather than defaulting to POST.
                method = tool.request_type.upper()
                if method == "GET":
                    response = await self._http_client.get(final_url, params=payload, headers=headers)
                else:
                    response = await self._http_client.request(method, final_url, json=payload, headers=headers)
                response.raise_for_status()

                # Handle 204 No Content responses that have no body
                if response.status_code == 204:
                    tool_result = ToolResult(content=[TextContent(type="text", text="Request completed successfully (No Content)")])
                elif response.status_code not in [200, 201, 202, 206]:
                    result = response.json()
                    tool_result = ToolResult(
                        content=[TextContent(type="text", text=str(result["error"]) if "error" in result else "Tool error encountered")],
                        is_error=True,
                    )
                else:
                    result = response.json()
                    filtered_response = extract_using_jq(result, tool.jsonpath_filter)
                    tool_result = ToolResult(content=[TextContent(type="text", text=json.dumps(filtered_response, indent=2))])

                success = True
            elif tool.integration_type == "MCP":
                transport = tool.request_type.lower()
                gateway = db.execute(select(DbGateway).where(DbGateway.id == tool.gateway_id).where(DbGateway.enabled)).scalar_one_or_none()
                headers = decode_auth(gateway.auth_value)

                async def connect_to_sse_server(server_url: str) -> str:
                    """
                    Connect to an MCP server running with SSE transport

                    Args:
                        server_url (str): MCP Server SSE URL

                    Returns:
                        str: Result of tool call
                    """
                    # Use async with directly to manage the context
                    async with sse_client(url=server_url, headers=headers) as streams:
                        async with ClientSession(*streams) as session:
                            # Initialize the session
                            await session.initialize()
                            tool_call_result = await session.call_tool(tool.original_name, arguments)
                    return tool_call_result

                async def connect_to_streamablehttp_server(server_url: str) -> str:
                    """
                    Connect to an MCP server running with Streamable HTTP transport

                    Args:
                        server_url (str): MCP Server URL

                    Returns:
                        str: Result of tool call
                    """
                    # Use async with directly to manage the context
                    async with streamablehttp_client(url=server_url, headers=headers) as (read_stream, write_stream, _get_session_id):
                        async with ClientSession(read_stream, write_stream) as session:
                            # Initialize the session
                            await session.initialize()
                            tool_call_result = await session.call_tool(tool.original_name, arguments)
                    return tool_call_result

                tool_gateway_id = tool.gateway_id
                tool_gateway = db.execute(select(DbGateway).where(DbGateway.id == tool_gateway_id).where(DbGateway.enabled)).scalar_one_or_none()

                tool_call_result = ToolResult(content=[TextContent(text="", type="text")])
                if transport == "sse":
                    tool_call_result = await connect_to_sse_server(tool_gateway.url)
                elif transport == "streamablehttp":
                    tool_call_result = await connect_to_streamablehttp_server(tool_gateway.url)
                content = tool_call_result.model_dump(by_alias=True).get("content", [])

                success = True
                filtered_response = extract_using_jq(content, tool.jsonpath_filter)
                tool_result = ToolResult(content=filtered_response)
            else:
                return ToolResult(content=[TextContent(type="text", text="Invalid tool type")])

            return tool_result
        except Exception as e:
            error_message = str(e)
            raise ToolInvocationError(f"Tool invocation failed: {error_message}")
        finally:
            await self._record_tool_metric(db, tool, start_time, success, error_message)

    async def update_tool(self, db: Session, tool_id: str, tool_update: ToolUpdate) -> ToolRead:
        """
        Update an existing tool.

        Args:
            db (Session): The SQLAlchemy database session.
            tool_id (str): The unique identifier of the tool.
            tool_update (ToolUpdate): Tool update schema with new data.

        Returns:
            The updated ToolRead object.

        Raises:
            ToolNotFoundError: If the tool is not found.
            IntegrityError: If there is a database integrity error.
            ToolError: For other update errors.

        Examples:
            >>> from mcpgateway.services.tool_service import ToolService
            >>> from unittest.mock import MagicMock, AsyncMock
            >>> from mcpgateway.schemas import ToolRead
            >>> service = ToolService()
            >>> db = MagicMock()
            >>> tool = MagicMock()
            >>> db.get.return_value = tool
            >>> db.commit = MagicMock()
            >>> db.refresh = MagicMock()
            >>> db.execute.return_value.scalar_one_or_none.return_value = None
            >>> service._notify_tool_updated = AsyncMock()
            >>> service._convert_tool_to_read = MagicMock(return_value='tool_read')
            >>> ToolRead.model_validate = MagicMock(return_value='tool_read')
            >>> import asyncio
            >>> asyncio.run(service.update_tool(db, 'tool_id', MagicMock()))
            'tool_read'
        """
        try:
            tool = db.get(DbTool, tool_id)
            if not tool:
                raise ToolNotFoundError(f"Tool not found: {tool_id}")
            if tool_update.name is not None:
                tool.name = tool_update.name
            if tool_update.url is not None:
                tool.url = str(tool_update.url)
            if tool_update.description is not None:
                tool.description = tool_update.description
            if tool_update.integration_type is not None:
                tool.integration_type = tool_update.integration_type
            if tool_update.request_type is not None:
                tool.request_type = tool_update.request_type
            if tool_update.headers is not None:
                tool.headers = tool_update.headers
            if tool_update.input_schema is not None:
                tool.input_schema = tool_update.input_schema
            if tool_update.annotations is not None:
                tool.annotations = tool_update.annotations
            if tool_update.jsonpath_filter is not None:
                tool.jsonpath_filter = tool_update.jsonpath_filter

            if tool_update.auth is not None:
                if tool_update.auth.auth_type is not None:
                    tool.auth_type = tool_update.auth.auth_type
                if tool_update.auth.auth_value is not None:
                    tool.auth_value = tool_update.auth.auth_value
            else:
                tool.auth_type = None

            tool.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(tool)
            await self._notify_tool_updated(tool)
            logger.info(f"Updated tool: {tool.name}")
            return self._convert_tool_to_read(tool)
        except IntegrityError as ie:
            logger.error(f"IntegrityError during tool update: {ie}")
            raise ie
        except Exception as ex:
            db.rollback()
            raise ToolError(f"Failed to update tool: {str(ex)}")

    async def _notify_tool_updated(self, tool: DbTool) -> None:
        """
        Notify subscribers of tool update.

        Args:
            tool: Tool updated
        """
        event = {
            "type": "tool_updated",
            "data": {"id": tool.id, "name": tool.name, "url": tool.url, "description": tool.description, "enabled": tool.enabled},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._publish_event(event)

    async def _notify_tool_activated(self, tool: DbTool) -> None:
        """
        Notify subscribers of tool activation.

        Args:
            tool: Tool activated
        """
        event = {
            "type": "tool_activated",
            "data": {"id": tool.id, "name": tool.name, "enabled": tool.enabled},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._publish_event(event)

    async def _notify_tool_deactivated(self, tool: DbTool) -> None:
        """
        Notify subscribers of tool deactivation.

        Args:
            tool: Tool deactivated
        """
        event = {
            "type": "tool_deactivated",
            "data": {"id": tool.id, "name": tool.name, "enabled": tool.enabled},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._publish_event(event)

    async def _notify_tool_deleted(self, tool_info: Dict[str, Any]) -> None:
        """
        Notify subscribers of tool deletion.

        Args:
            tool_info: Dictionary on tool deleted
        """
        event = {
            "type": "tool_deleted",
            "data": tool_info,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._publish_event(event)

    async def subscribe_events(self) -> AsyncGenerator[Dict[str, Any], None]:
        """Subscribe to tool events.

        Yields:
            Tool event messages.
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._event_subscribers.append(queue)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            self._event_subscribers.remove(queue)

    async def _notify_tool_added(self, tool: DbTool) -> None:
        """
        Notify subscribers of tool addition.

        Args:
            tool: Tool added
        """
        event = {
            "type": "tool_added",
            "data": {
                "id": tool.id,
                "name": tool.name,
                "url": tool.url,
                "description": tool.description,
                "enabled": tool.enabled,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._publish_event(event)

    async def _notify_tool_removed(self, tool: DbTool) -> None:
        """
        Notify subscribers of tool removal (soft delete/deactivation).

        Args:
            tool: Tool removed
        """
        event = {
            "type": "tool_removed",
            "data": {"id": tool.id, "name": tool.name, "enabled": tool.enabled},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._publish_event(event)

    async def _publish_event(self, event: Dict[str, Any]) -> None:
        """
        Publish event to all subscribers.

        Args:
            event: Event to publish
        """
        for queue in self._event_subscribers:
            await queue.put(event)

    async def _validate_tool_url(self, url: str) -> None:
        """Validate tool URL is accessible.

        Args:
            url: URL to validate.

        Raises:
            ToolValidationError: If URL validation fails.
        """
        try:
            response = await self._http_client.get(url)
            response.raise_for_status()
        except Exception as e:
            raise ToolValidationError(f"Failed to validate tool URL: {str(e)}")

    async def _check_tool_health(self, tool: DbTool) -> bool:
        """Check if tool endpoint is healthy.

        Args:
            tool: Tool to check.

        Returns:
            True if tool is healthy.
        """
        try:
            response = await self._http_client.get(tool.url)
            return response.is_success
        except Exception:
            return False

    async def event_generator(self) -> AsyncGenerator[Dict[str, Any], None]:
        """Generate tool events for SSE.

        Yields:
            Tool events.
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._event_subscribers.append(queue)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            self._event_subscribers.remove(queue)

    # --- Metrics ---
    async def aggregate_metrics(self, db: Session) -> Dict[str, Any]:
        """
        Aggregate metrics for all tool invocations across all tools.

        Args:
            db: Database session

        Returns:
            Aggregated metrics computed from all ToolMetric records.

        Examples:
            >>> from mcpgateway.services.tool_service import ToolService
            >>> from unittest.mock import MagicMock
            >>> service = ToolService()
            >>> db = MagicMock()
            >>> db.execute.return_value.scalar.return_value = 0
            >>> import asyncio
            >>> result = asyncio.run(service.aggregate_metrics(db))
            >>> isinstance(result, dict)
            True
        """

        total = db.execute(select(func.count(ToolMetric.id))).scalar() or 0  # pylint: disable=not-callable
        successful = db.execute(select(func.count(ToolMetric.id)).where(ToolMetric.is_success)).scalar() or 0  # pylint: disable=not-callable
        failed = db.execute(select(func.count(ToolMetric.id)).where(not_(ToolMetric.is_success))).scalar() or 0  # pylint: disable=not-callable
        failure_rate = failed / total if total > 0 else 0.0
        min_rt = db.execute(select(func.min(ToolMetric.response_time))).scalar()
        max_rt = db.execute(select(func.max(ToolMetric.response_time))).scalar()
        avg_rt = db.execute(select(func.avg(ToolMetric.response_time))).scalar()
        last_time = db.execute(select(func.max(ToolMetric.timestamp))).scalar()

        return {
            "total_executions": total,
            "successful_executions": successful,
            "failed_executions": failed,
            "failure_rate": failure_rate,
            "min_response_time": min_rt,
            "max_response_time": max_rt,
            "avg_response_time": avg_rt,
            "last_execution_time": last_time,
        }

    async def reset_metrics(self, db: Session, tool_id: Optional[int] = None) -> None:
        """
        Reset all tool metrics by deleting all records from the tool metrics table.

        Args:
            db: Database session
            tool_id: Optional tool ID to reset metrics for a specific tool

        Examples:
            >>> from mcpgateway.services.tool_service import ToolService
            >>> from unittest.mock import MagicMock
            >>> service = ToolService()
            >>> db = MagicMock()
            >>> db.execute = MagicMock()
            >>> db.commit = MagicMock()
            >>> import asyncio
            >>> asyncio.run(service.reset_metrics(db))
        """

        if tool_id:
            db.execute(delete(ToolMetric).where(ToolMetric.tool_id == tool_id))
        else:
            db.execute(delete(ToolMetric))
        db.commit()
