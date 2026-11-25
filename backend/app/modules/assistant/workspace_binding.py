"""Explicit Workspace delegation input, not invocation authority.

A selected thread alone grants nothing. Persisted identity/current authorization
are checked by Host intake and execution, never by this pure request contract.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.modules.assistant.core_adapter import digest
from backend.app.modules.workspace.schemas import ArtifactKind


class ArtifactDelegation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: ArtifactKind
    operations: list[Literal['read', 'create', 'patch']] = Field(min_length=1, max_length=3)

    @field_validator('operations')
    @classmethod
    def explicit_operations(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('workspace_operations_duplicate')
        if ('create' in value or 'patch' in value) and 'read' not in value:
            raise ValueError('workspace_read_required')
        return sorted(value)


class WorkspaceContextRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    thread_id: str = Field(min_length=1, max_length=64)
    artifacts: list[ArtifactDelegation] = Field(min_length=1, max_length=5)

    @model_validator(mode='after')
    def unique_kinds(self):
        if len({entry.kind for entry in self.artifacts}) != len(self.artifacts):
            raise ValueError('workspace_kind_duplicate')
        self.artifacts.sort(key=lambda entry: entry.kind)
        return self


def normalize_workspace_context(value):
    """Return owned JSON data; no DB facts or permission decisions are minted."""
    if value is None:
        return None
    return WorkspaceContextRequest.model_validate(value).model_dump(mode='json')


def work_request_digest(text, workspace_context=None):
    """NULL preserves the exact legacy request hash, including historic replays."""
    request = {'text': text}
    context = normalize_workspace_context(workspace_context)
    if context is not None:
        request['workspace_context'] = context
    return digest(request)


def _deny():
    from fastapi import HTTPException
    raise HTTPException(403, 'assistant_workspace_binding_denied')


def context_sha256(context):
    """Pin resource identity/delegation, never non-security UI layout version."""
    if context is None:
        return None
    fields = {'version', 'root_work_id', 'organization_id', 'store_id', 'owner_id',
              'thread_id', 'thread_binding_id', 'thread_binding_version', 'artifacts'}
    if type(context) is not dict or set(context) != fields or type(context['version']) is not int or context['version'] != 1:
        _deny()
    if (any(type(context[key]) is not str or not context[key] for key in
            ('root_work_id', 'organization_id', 'store_id', 'owner_id', 'thread_binding_id'))
            or type(context['thread_binding_version']) is not int or context['thread_binding_version'] < 1):
        _deny()
    request = normalize_workspace_context({key: context[key] for key in ('thread_id', 'artifacts')})
    if request != {key: context[key] for key in ('thread_id', 'artifacts')}:
        _deny()
    return digest({key: value for key, value in context.items() if key != 'thread_binding_version'})


def context_request(context):
    if context is None:
        return None
    context_sha256(context)
    return {key: context[key] for key in ('thread_id', 'artifacts')}


def context_view(context):
    if context is None:
        return None
    from copy import deepcopy
    return deepcopy({key: context[key] for key in ('thread_id', 'thread_binding_id', 'thread_binding_version', 'artifacts')}) | {
        'sha256': context_sha256(context)}


def current_thread(session, context, *, lock=False):
    from sqlalchemy import select
    from backend.app.modules.workspace.models import WorkspaceThreadBinding
    statement = select(WorkspaceThreadBinding).where(
        WorkspaceThreadBinding.id == context['thread_binding_id'],
        WorkspaceThreadBinding.thread_id == context['thread_id'],
        WorkspaceThreadBinding.organization_id == context['organization_id'],
        WorkspaceThreadBinding.store_id == context['store_id'],
        WorkspaceThreadBinding.subject_id == context['owner_id'])
    if lock:
        statement = statement.with_for_update()
    with session.no_autoflush:
        row = session.scalar(statement.execution_options(populate_existing=True))
    if row is None:
        _deny()
    return row


def selected_permissions(session, actor, context):
    """Each kind/action has OR alternatives, not an accidental all-permissions AND."""
    from backend.app.modules.identity.service import resolve_effective_capabilities
    from backend.app.modules.assistant.principal import WorkPrincipal, projection_permissions
    from backend.app.modules.workspace.service import _KIND_PERMISSION_MAP
    current = (projection_permissions(session, actor) if type(actor) is WorkPrincipal else
               frozenset(resolve_effective_capabilities(session, actor)[0]))
    permissions = set()
    for entry in context['artifacts']:
        for operation in entry['operations']:
            accepted = set(_KIND_PERMISSION_MAP[entry['kind']]['read' if operation == 'read' else 'write'])
            allowed = accepted.intersection(current)
            if not allowed:
                _deny()
            permissions.update(allowed)
    return frozenset(permissions)


def resolve_context(session, actor, value, root_work_id):
    """Human intake only: resolve the real persisted binding in the same root."""
    from sqlalchemy import select
    from backend.app.core.security import AuthenticatedSubject
    from backend.app.modules.workspace.models import WorkspaceThreadBinding
    request = normalize_workspace_context(value)
    if request is None:
        return None
    if type(actor) is not AuthenticatedSubject:
        _deny()
    binding = session.scalar(select(WorkspaceThreadBinding).where(
        WorkspaceThreadBinding.thread_id == request['thread_id'],
        WorkspaceThreadBinding.organization_id == actor.organization_id,
        WorkspaceThreadBinding.store_id == actor.store_id,
        WorkspaceThreadBinding.subject_id == actor.subject_id).with_for_update())
    if binding is None:
        _deny()
    context = dict(version=1, root_work_id=root_work_id, organization_id=actor.organization_id,
        store_id=actor.store_id, owner_id=actor.subject_id, thread_id=binding.thread_id,
        thread_binding_id=binding.id, thread_binding_version=binding.version, artifacts=request['artifacts'])
    context_sha256(context)
    selected_permissions(session, actor, context)
    return context


def root_context(session, work, *, kind='work'):
    from backend.app.modules.assistant.models import AssistantWork
    with session.no_autoflush:
        root = work if kind == 'work' else session.get(AssistantWork, work.root_work_id, populate_existing=True)
    if root is None or (root.organization_id, root.store_id, root.owner_id) != (
            work.organization_id, work.store_id, work.owner_id):
        _deny()
    context = root.workspace_context
    if context is not None:
        context_sha256(context)
        if (context['root_work_id'], context['organization_id'], context['store_id'], context['owner_id']) != (
                root.task_id, root.organization_id, root.store_id, root.owner_id):
            _deny()
    return context


def validate_origin(session, row):
    """Immutable provenance only; terminal readers must not reauthorize execution."""
    from backend.app.modules.assistant.authority import _models, row_kind
    kind = row_kind(row)
    with session.no_autoflush:
        work = session.get(_models(kind)[0], row.work_id, populate_existing=True)
    if work is None or (work.organization_id, work.store_id, work.owner_id) != (
            row.organization_id, row.store_id, row.owner_id):
        _deny()
    context = root_context(session, work, kind=kind)
    if context_sha256(context) != row.workspace_binding_sha256:
        _deny()
    return context


def augment_policy(session, actor, policy, context):
    from dataclasses import replace
    from backend.app.modules.assistant.authority import WorkAuthorityPolicy
    if context is None:
        return policy
    if type(policy) is not WorkAuthorityPolicy:
        _deny()
    with session.no_autoflush:
        current_thread(session, context, lock=True)
        permissions = selected_permissions(session, actor, context)
    return replace(policy, permissions=tuple(sorted(set(policy.permissions) | permissions)))
