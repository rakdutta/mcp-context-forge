# -*- coding: utf-8 -*-
"""
REST Passthrough Plugin Framework

Copyright 2025
SPDX-License-Identifier: Apache-2.0

This module provides a flexible plugin framework for REST passthrough endpoints in MCP Gateway.

Features:
- Pre-processing hooks (on_passthrough_request) for validation, authentication, rate limiting, and request mutation.
- Post-processing hooks (on_passthrough_response) for response redaction, transformation, caching, and auditing.
- Built-in plugins: pii_filter (PII redaction), deny_filter (request denial), regex_filter (regex-based filtering), resource_filter (resource policy enforcement).
- Configurable plugin chains per tool or route, supporting dynamic composition and extension.

Usage:
- Register plugin functions in PLUGIN_REGISTRY.
- Use on_passthrough_request and on_passthrough_response to apply chains in the router pipeline.
- Extend with custom plugins as needed for cross-cutting concerns.
"""

import re


# Individual plugin functions
def pii_filter(context, request):
    """
    Redact email addresses from request body fields.
    Args:
        context: Plugin context (dict, may include tool/user info).
        request: The request object (dict) to be filtered.
    Returns:
        The request object with email addresses replaced by [REDACTED].
    """
    import re
    if isinstance(request, dict):
        body = request.get("body")
        if isinstance(body, dict):
            for k, v in body.items():
                if isinstance(v, str):
                    body[k] = re.sub(r"[\w\.-]+@[\w\.-]+", "[REDACTED]", v)
    return request

def deny_filter(context, request):
    """
    Deny the request if the 'X-Forbidden' header is present.
    Args:
        context: Plugin context (dict).
        request: The request object (dict).
    Raises:
        HTTPException: If 'X-Forbidden' header is found.
    Returns:
        The original request if not denied.
    """
    headers = request.get("headers", {}) if isinstance(request, dict) else {}
    if "X-Forbidden" in headers:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Request denied by deny_filter plugin.")
    return request

def regex_filter(context, obj):
    """
    Remove all digits from the body of a request or response.
    Args:
        context: Plugin context (dict).
        obj: The request or response object (dict or Response).
    Returns:
        The object with digits removed from its body.
    """
    import re
    if hasattr(obj, "body") and isinstance(obj.body, (bytes, str)):
        if isinstance(obj.body, bytes):
            obj.body = re.sub(rb"\d", b"", obj.body)
        else:
            obj.body = re.sub(r"\d", "", obj.body)
    elif isinstance(obj, dict):
        body = obj.get("body")
        if isinstance(body, str):
            obj["body"] = re.sub(r"\d", "", body)
    return obj

def resource_filter(context, obj):
    """
    Resource policy enforcement stub. Currently allows all requests/responses.
    Args:
        context: Plugin context (dict).
        obj: The request or response object.
    Returns:
        The original object (no changes).
    """
    return obj

# Plugin registry for chaining
PLUGIN_REGISTRY = {
    "pii_filter": pii_filter,
    "deny_filter": deny_filter,
    "regex_filter": regex_filter,
    "resource_filter": resource_filter,
}

# Pre-hook: chain plugins by name
def on_passthrough_request(context, request, chain=None):
    """
    Apply a chain of pre-processing plugins to the incoming request.
    Each plugin can mutate, validate, or deny the request.
    Args:
        context: Plugin context (dict, may include tool/user info).
        request: The incoming request object (dict).
        chain: List of plugin names to apply (optional, defaults to all).
    Returns:
        The processed request object after all plugins have run.
    Raises:
        HTTPException: If any plugin denies the request.
    """
    chain = chain or ["deny_filter", "pii_filter", "regex_filter", "resource_filter"]
    for plugin_name in chain:
        plugin_func = PLUGIN_REGISTRY.get(plugin_name)
        if plugin_func:
            request = plugin_func(context, request)
    return request


# Post-hook: chain plugins by name
def on_passthrough_response(context, request, response, chain=None):
    """
    Apply a chain of post-processing plugins to the outgoing response.
    Each plugin can mutate, redact, or transform the response.
    Args:
        context: Plugin context (dict).
        request: The original request object (dict).
        response: The outgoing response object (dict or Response).
        chain: List of plugin names to apply (optional, defaults to all).
    Returns:
        The processed response object after all plugins have run.
    """
    chain = chain or ["regex_filter", "resource_filter"]
    for plugin_name in chain:
        plugin_func = PLUGIN_REGISTRY.get(plugin_name)
        if plugin_func:
            response = plugin_func(context, response)
    return response

# Add more plugin functions here as needed
