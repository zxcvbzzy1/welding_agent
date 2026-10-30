from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str
    display_name: str = ""
    avatar_url: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str


class ContentPartRequest(BaseModel):
    type: Literal["text", "code", "image", "file", "web_preview", "diff", "deploy", "event_ref"] = "text"
    text: str = ""
    language: str = ""
    url: str = ""
    name: str = ""
    mime_type: str = ""
    size: int = 0
    artifact_id: str = ""
    file_id: str = ""
    diff: str = ""
    title: str = ""
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoomCreateRequest(BaseModel):
    type: Literal["group"] = "group"
    title: str = ""
    member_agent_ids: list[str] = Field(default_factory=list)
    created_by: str = "user"
    avatar_url: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoomUpdateRequest(BaseModel):
    title: str | None = None
    avatar_url: str | None = None
    member_agent_ids: list[str] | None = None
    metadata: dict[str, Any] | None = None


class AgentCreateRequest(BaseModel):
    name: str
    agent_type: Literal["executor", "planner"] = "executor"
    context_id: str = "default_executor"
    role_prompt: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    tool_names: list[str] = Field(default_factory=list)
    tool_fields: list[str] = Field(default_factory=list)


class AgentUpdateRequest(BaseModel):
    # 全部可选：只更新本次传入的字段（None 表示不改）。
    name: str | None = None
    role_prompt: str | None = None
    metadata: dict[str, Any] | None = None
    tool_names: list[str] | None = None
    tool_fields: list[str] | None = None


class AgentConversationCreateRequest(BaseModel):
    title: str = ""
    avatar_url: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoomConversationCreateRequest(BaseModel):
    title: str = ""


class MessageCreateRequest(BaseModel):
    sender_type: Literal["user", "agent", "system"] = "user"
    sender_id: str = "user"
    content_parts: list[ContentPartRequest]
    conversation_id: str = ""
    mentions: list[str] = Field(default_factory=list)
    reply_to: str = ""
    quote_of: str = ""
    run_id: str = ""
    status: str = "sent"
    metadata: dict[str, Any] = Field(default_factory=dict)


class DispatchRequest(BaseModel):
    message_id: str
    planner_agent_id: str = "default_planner"
    context_id: str = "default_step"
    max_replan_rounds: int = 3
    auto_start: bool = True


class ReplyRequest(BaseModel):
    message_id: str
    auto_start: bool = True


class MessageActionRequest(BaseModel):
    action_type: Literal["reply", "quote", "copy", "expand", "apply_diff"]
    actor_id: str = "user"
    payload: dict[str, Any] = Field(default_factory=dict)


class ArtifactUploadRequest(BaseModel):
    filename: str
    content_base64: str
    content_type: str = "application/octet-stream"


class ConversationUpdateRequest(BaseModel):
    pinned: bool | None = None
    archived: bool | None = None
    title: str | None = None
    memory_write_enabled: bool | None = Field(default=None, strict=True)


class RegenerateRequest(BaseModel):
    auto_start: bool = True


class FavoriteCreateRequest(BaseModel):
    scope_type: Literal["conversation", "room"]
    scope_id: str
    content: str
    title: str = ""
    source_message_id: str = ""


class FavoriteUpdateRequest(BaseModel):
    title: str | None = None
    content: str | None = None
    enabled: bool | None = None


class MessageFavoriteRequest(BaseModel):
    title: str = ""


class AgentBuilderMessage(BaseModel):
    role: Literal["user", "assistant"] = "user"
    content: str = ""


class AgentBuilderDraft(BaseModel):
    """对话式创建 agent 的草稿，字段对齐前端创建表单（agentForm），而非 AgentCreateRequest。

    description / workdir 在前端落到 metadata；这里整体
    镜像表单，前端把草稿应用到表单后仍走原有 createAgent 流程。
    """

    name: str = ""
    agent_type: Literal["executor", "planner"] = "executor"
    model_kind: Literal["llm", "vlm"] = "llm"
    description: str = ""
    role_prompt: str = ""
    workdir: str = ""
    tool_names: list[str] = Field(default_factory=list)
    tool_fields: list[str] = Field(default_factory=list)


class AgentBuilderChatRequest(BaseModel):
    messages: list[AgentBuilderMessage] = Field(default_factory=list)
    draft: AgentBuilderDraft | None = None


class ArtifactBundleRequest(BaseModel):
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    filename: str = "artifacts.zip"
