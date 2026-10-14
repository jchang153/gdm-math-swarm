"""Conference tool definitions exposed to experimental agents.

The five communication/feedback descriptions are reproduced from Paglieri et al.,
arXiv 2609.04170, Appendix C (CC BY 4.0), with the paper's display line wraps
joined and typeset apostrophes restored to ASCII. Input schemas and the seven
reconstructed tools are this project's wording; see docs/prompts.md.

A definition may carry ``sandbox_file`` (docs/interfaces.md): the runtime adapter
reads ``arg`` from the calling agent's own container, enforces ``allowed_roots``
and ``max_bytes`` (and ``allowed_suffixes`` when present), and passes the content
to the conference service as ``args[into]`` together with the original path.
"""
from __future__ import annotations

from .conference import (DEFAULT_PAGE_LIMIT, INTENT_TYPES, LIBRARY_KINDS, MAX_NOTEBOOK_BYTES,
                         MAX_PAGE_LIMIT, MAX_READ_CHARS, MAX_SOURCE_BYTES, MAX_TAG_CHARS, MAX_TAGS,
                         MAX_TEXT_CHARS)

PAPER = "paper_appendix_c"
RECONSTRUCTED = "reconstructed"



def _paging():
    """Reconstructed paging parameters for bounded listing output (docs/prompts.md)."""
    return {
        "since_seq": {"type": "integer", "minimum": 0,
                      "description": "Optional: return only entries whose seq is greater than this value, "
                                     "oldest first. Use next_since_seq from an earlier result to continue "
                                     "or to see only newer entries."},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_LIMIT,
                  "description": f"Optional: maximum number of entries to return (default "
                                 f"{DEFAULT_PAGE_LIMIT}). Without since_seq the most recent entries are "
                                 "returned. With limit 1 an entry's text is never shortened."},
    }


TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "post_intent",
        "provenance": PAPER,
        "description": (
            "Post on the shared research bulletin board. Share what you're exploring, building, "
            "or proposing — so other agents know what you're thinking about. Use 'exploring' for "
            "directions you're investigating, 'building' for stepping stones in progress, "
            "'contribution' for results you've found, or 'proposing' for new problems or "
            "questions. Be as vague or specific as you like."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent_type": {"type": "string", "enum": list(INTENT_TYPES),
                                "description": "One of exploring, building, contribution, proposing."},
                "message": {"type": "string", "maxLength": MAX_TEXT_CHARS,
                            "description": "The text of the post."},
                "tags": {"type": "array", "items": {"type": "string", "maxLength": MAX_TAG_CHARS},
                         "description": f"Optional tags (at most {MAX_TAGS}), for example problem "
                                        "identifiers or topics."},
            },
            "required": ["intent_type", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_bulletin_board",
        "provenance": PAPER,
        "description": (
            "Read the shared research bulletin board. See what other agents are exploring, "
            "building, and proposing. Filter by intent_type, agent_id, or tag. Use this to find "
            "collaborators, avoid redundant work, and discover stepping stones you can build on."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent_type": {"type": "string", "enum": list(INTENT_TYPES),
                                "description": "Optional: only posts with this intent type."},
                "agent_id": {"type": "string",
                             "description": "Optional: only posts by this agent identifier."},
                "tag": {"type": "string", "description": "Optional: only posts carrying this tag."},
                **_paging(),
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "send_direct_message",
        "provenance": PAPER,
        "description": "Forwards a direct message to an agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string",
                             "description": "Identifier of the recipient agent, for example agent-007."},
                "message": {"type": "string", "maxLength": MAX_TEXT_CHARS,
                            "description": "The message text."},
            },
            "required": ["agent_id", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_messages",
        "provenance": PAPER,
        "description": "Retrieves all received direct messages.",
        # seq numbers the caller's own inbox (1, 2, ...); message_id is opaque.
        "inputSchema": {"type": "object", "properties": _paging(), "required": [],
                        "additionalProperties": False},
    },
    {
        "name": "submit_feedback",
        "provenance": PAPER,
        "description": (
            "Submit feedback, complaints, bug reports, or suggestions about the conference "
            "directly to the organizers. Use this whenever you encounter something that feels "
            "unfair, confusing, broken, or could be improved — or to praise something that works "
            "well. This is NOT a paper review; it's a private channel to the conference "
            "organizers. Your feedback helps improve the process for everyone."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "maxLength": MAX_TEXT_CHARS,
                            "description": "Your feedback for the conference organizers."},
            },
            "required": ["message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_problems",
        "provenance": RECONSTRUCTED,
        "description": (
            "List all conference problems with their status. Closed problems show the credited "
            "agent and the acceptance time."),
        "inputSchema": {"type": "object", "properties": _paging(), "required": [],
                        "additionalProperties": False},
    },
    {
        "name": "get_problem",
        "provenance": RECONSTRUCTED,
        "description": (
            "Get a conference problem's Lean template, including its fixed statement and editable "
            "regions, together with its current status."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem_id": {"type": "string", "description": "The problem identifier."},
            },
            "required": ["problem_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_solution",
        "provenance": RECONSTRUCTED,
        "description": (
            "Submit a complete Lean solution file for an open problem. Give the problem identifier "
            "and the path of the file in your workspace. The first accepted submission for a problem "
            "is credited to its author, closes the problem, and is published to the shared library. "
            "A submission that is not accepted is returned with the reason and the compiler output."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem_id": {"type": "string", "description": "The problem identifier."},
                "file_path": {"type": "string",
                              "description": "Path of your complete Lean file under /workspace (a relative "
                                             "path is taken relative to /workspace)."},
            },
            "required": ["problem_id", "file_path"],
            "additionalProperties": False,
        },
        "sandbox_file": {"arg": "file_path", "into": "source", "allowed_roots": ["/workspace"],
                         "max_bytes": MAX_SOURCE_BYTES},
    },
    {
        "name": "list_library",
        "provenance": RECONSTRUCTED,
        "description": (
            "List the files in the shared library: accepted solutions, with their authors and "
            "acceptance times, and notebooks that agents have published."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(LIBRARY_KINDS),
                         "description": "Optional: only solutions or only notebooks."},
                "agent_id": {"type": "string", "description": "Optional: only files by this agent."},
                **_paging(),
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_library_file",
        "provenance": RECONSTRUCTED,
        "description": (
            "Read a file from the shared library. A long file is returned in consecutive parts of at most "
            f"{MAX_READ_CHARS} characters; each result gives the file's total length and, when more "
            "remains, the offset at which to continue."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The library path shown by list_library."},
                "revision": {"type": "integer", "minimum": 1,
                             "description": "Optional earlier revision of a notebook; defaults to the latest."},
                "offset": {"type": "integer", "minimum": 0,
                           "description": "Optional: character position to start reading from (default 0). "
                                          "Use next_offset from an earlier result to continue."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_READ_CHARS,
                          "description": f"Optional: maximum number of characters to return (default "
                                         f"{MAX_READ_CHARS})."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "publish_notebook",
        "provenance": RECONSTRUCTED,
        "description": (
            "Publish one of your markdown notes, such as a wiki page under /tmp/w/memory/wiki/ or "
            "/workspace/RESEARCH.md, to the shared library so that all agents can read it. "
            "Publishing the same file again adds a new revision."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string",
                              "description": "Path of a .md file under /tmp/w/memory/wiki/ or /workspace/ "
                                             "(a relative path is taken relative to /workspace)."},
            },
            "required": ["file_path"],
            "additionalProperties": False,
        },
        "sandbox_file": {"arg": "file_path", "into": "content",
                         "allowed_roots": ["/tmp/w/memory/wiki", "/workspace"],
                         "allowed_suffixes": [".md"], "max_bytes": MAX_NOTEBOOK_BYTES},
    },
    {
        "name": "get_conference_status",
        "provenance": RECONSTRUCTED,
        "description": (
            "Show the conference status: the number of open problems, the credited agent and "
            "acceptance time for each closed problem, and the number of accepted problems for each "
            "participating agent."),
        "inputSchema": {"type": "object", "properties": {}, "required": [],
                        "additionalProperties": False},
    },
]

TOOL_NAMES = tuple(definition["name"] for definition in TOOL_DEFINITIONS)
