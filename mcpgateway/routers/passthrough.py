# -*- coding: utf-8 -*-
"""
REST Passthrough API Router

Copyright 2025
SPDX-License-Identifier: Apache-2.0

This module provides a secure and extensible REST passthrough endpoint for MCP Gateway.

Features:
- Forwards requests to upstream REST APIs with dynamic tool resolution
- Supports pre- and post-processing plugin chains for validation, transformation, redaction, and auditing
- Security: host allowlist, SSRF protection (private IP block), header redaction
- Configurable per-tool plugin chains and allowlists
- Extensible via plugin framework in mcpgateway.plugins.passthrough_plugins

Usage:
- Define tools with passthrough config, allowlist, and plugin chains
- Register plugins in PLUGIN_REGISTRY and configure chains in settings or per-tool
- Use this router to expose secure passthrough endpoints for REST APIs

See also:
- mcpgateway.plugins.passthrough_plugins for plugin implementation and chaining
- mcpgateway.config for passthrough configuration
"""

from urllib.parse import urlparse
import ipaddress
import socket

from fastapi import APIRouter, Request, Response, HTTPException
import httpx
from mcpgateway.config import settings
from mcpgateway.services.tool_service import ToolNotFoundError, ToolService
from mcpgateway.db import get_db

from mcpgateway.plugins.passthrough_plugins import on_passthrough_request, on_passthrough_response


tool_service = ToolService()
router = APIRouter(prefix=settings.passthrough_config["base_path"])


def is_upstream_allowed(url, ALLOWED_SCHEMES, ALLOWED_HOSTS):
    """
    Check if the upstream URL's scheme and hostname are allowed.
    Args:
        url: The upstream URL to check.
        ALLOWED_SCHEMES: Set of allowed URL schemes (e.g., {"https"}).
        ALLOWED_HOSTS: Set of allowed hostnames.
    Returns:
        True if both scheme and hostname are allowed, False otherwise.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        return False
    if parsed.hostname not in ALLOWED_HOSTS:
        return False
    return True

def is_public_ip(hostname):
    """
    Check if the given hostname resolves to a public IP address.
    Args:
        hostname: Hostname to resolve and check.
    Returns:
        True if IP is public (not private, loopback, or reserved), False otherwise.
    """
    try:
        ip = socket.gethostbyname(hostname)
        ip_obj = ipaddress.ip_address(ip)
        return not (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved)
    except Exception:
        return False

def redact_headers(headers):
    """
    Redact sensitive headers (e.g., Authorization, Cookie) from a headers dict.
    Args:
        headers: Dictionary of HTTP headers.
    Returns:
        New dictionary with sensitive headers replaced by [REDACTED].
    """
    redacted = {}
    for k, v in headers.items():
        if k.lower() in {"authorization", "cookie"}:
            redacted[k] = "[REDACTED]"
        else:
            redacted[k] = v
    return redacted

async def map_request(tool, request):
    """
    Map and transform the incoming FastAPI request for upstream forwarding.
    Applies tool-specific header and query mappings.
    Args:
        tool: Tool object with mapping config.
        request: FastAPI Request object.
    Returns:
        Dict with method, headers, params, and body for upstream request.
    """
    mapped = {}
    mapped["method"] = request.method
    # Apply header_mapping if present
    headers = dict(request.headers)
    if tool.header_mapping:
        for k, v in tool.header_mapping.items():
            # If value is a string, use it directly; if it's a key, map from incoming headers
            if isinstance(v, str):
                headers[k] = v
            elif v in headers:
                headers[k] = headers[v]
    mapped["headers"] = headers

    # Apply query_mapping if present
    params = dict(request.query_params)
    if tool.query_mapping:
        for k, v in tool.query_mapping.items():
            # If value is a string, use it directly; if it's a key, map from incoming params
            if isinstance(v, str):
                params[k] = v
            elif v in params:
                params[k] = params[v]
    mapped["params"] = params

    if request.method in ("POST", "PUT", "PATCH"):
        mapped["body"] = await request.json()
    else:
        mapped["body"] = None
    return mapped

@router.api_route(
    "/{tool_id}{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def passthrough_endpoint(
    tool_id: str,
    path: str = "",
    db: Session = Depends(get_db),
    request: Request = None,
    user: str = Depends(require_auth),
    tool_service: ToolService = Depends(get_tool_service),
):
    """
    REST passthrough endpoint for forwarding requests to upstream APIs with plugin support.
    Resolves tool, applies security checks, runs pre/post plugin chains, and returns upstream response.
    Args:
        tool_id: Tool identifier from path.
        path: Additional path to append to base_url.
        db: Database session (FastAPI dependency).
        request: Incoming FastAPI Request object.
        user: Authenticated user (FastAPI dependency).
        tool_service: ToolService instance (FastAPI dependency).
    Returns:
        FastAPI Response object with upstream API result.
    Raises:
        HTTPException: For tool not found, security violations, or upstream errors.
    """
    # Build upstream URL
    try:
        tool = await tool_service.get_tool(db, tool_id=tool_id)
    except ToolNotFoundError:
        raise HTTPException(status_code=404, detail="Tool not found")

    if not tool or not tool.expose_passthrough:
        raise HTTPException(status_code=404, detail="Tool not found or passthrough not enabled")

    base_url = tool["base_url"]
    path_template = tool.get("path_template", "")
    upstream_path = path or path_template  
    upstream_url = f"{base_url}{upstream_path}"
    # Load allowed hosts from tool.allowlist if present, else fallback to config or default
    ALLOWED_HOSTS = set(getattr(tool, "allowlist", []))
    if not ALLOWED_HOSTS:
        ALLOWED_HOSTS = set(getattr(settings, "passthrough_allowed_hosts", []))
    if not ALLOWED_HOSTS:
        ALLOWED_HOSTS = {"api.github.com", "api.example.com"}  # Fallback default
    ALLOWED_SCHEMES = {"https"}
    # Security: Enforce upstream host/scheme allowlist
    if not is_upstream_allowed(upstream_url, ALLOWED_SCHEMES, ALLOWED_HOSTS):
        raise HTTPException(status_code=403, detail="Upstream host or scheme not allowed")

    # Security: Block private IP ranges (SSRF protection)
    parsed = urlparse(upstream_url)
    if not is_public_ip(parsed.hostname):
        raise HTTPException(status_code=403, detail="Upstream IP is private or loopback")

    mapped_request = await map_request(tool, request)
    
    # Prepare request data
    method = mapped_request["method"]
    headers = mapped_request["headers"]
    query_params = mapped_request["params"]
    body = mapped_request["body"]

    # Run pre-plugins (prefer tool-level chain if present)
    pre_chain = getattr(tool, "plugin_chain_pre", None)
    if not pre_chain:
        pre_chain = settings.passthrough_config["default_plugin_chains"]["pre"]
    context = {"tool_id": tool_id}
    request = await request.json() if request.method in ("POST", "PUT", "PATCH") else None
    mapped_request = await map_request(tool, request)
    mapped_request = on_passthrough_request(context, mapped_request, chain=pre_chain)

    # Forward request to upstream
    async with httpx.AsyncClient(timeout=settings.passthrough_config["default_timeout_ms"] / 1000) as client:
        resp = await client.request(
            method,
            upstream_url,
            headers=headers,
            params=query_params,
            content=body,
        )

    # Run post-plugins (prefer tool-level chain if present)
    post_chain = getattr(tool, "plugin_chain_post", None)
    if not post_chain:
        post_chain = settings.passthrough_config["default_plugin_chains"]["post"]
    resp = on_passthrough_response(context, mapped_request, resp, chain=post_chain)

    # Return response
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
        media_type=resp.headers.get("content-type"),
    )

    