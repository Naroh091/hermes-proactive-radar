"""Tool firewall for delegated jobs: the lock behind the prompt.

A delegated ``hermes chat`` child is told what it may do (reversible and private effects only for
``read`` and ``local`` steps), but a prompt is advice. On 2026-10-21 a ``local`` step whose draft
tool kept failing reached for ``reply_all`` and mailed a thread the owner had not approved. This
module is the guarantee the prompt cannot give: inside a job every tool whose name denotes an effect
on third parties, or one that cannot be undone, is refused before it runs, through the host's
``pre_tool_call`` hook, unless the step is an ``external`` one the owner consented to and the tool
is on that step's allow list.

Which session is a job is known from the mark in its prompt (``radar.jobs.mark``), bound to the
session id when the job's first LLM call fires; the environment variables (``HERMES_RADAR_JOB``,
``HERMES_RADAR_JOB_CLASS``, ``HERMES_RADAR_JOB_ALLOW``) remain as a fallback for a host that runs
the child's hooks in the child's own process.

Tools are judged by the last segment of their name (``mcp__gmail_work__reply_all`` → ``reply_all``),
so every mailbox, calendar, chat or repository server is covered without naming servers. Inside a job
the rule is deny by default: a tool passes when it is known to only look, when it is one of the host's
working tools, or when the step's class or its consent grants it. A subagent (``delegate_task``) has a
session of its own that carries no mark, so a job cannot spawn one. Shell commands and ``execute_code``
are read for what they would do to third parties (a merge through ``gh``, a mail through ``gws``, an
upload through ``curl``) and for the owner's secrets.

Reading a command is a net, not a wall: a script the job writes and runs, or an ``ssh``, cannot be told
apart from harmless work, and the browser is not judged at all. The prompt and the owner's consent still
govern those.
"""
from __future__ import annotations

import os
import re
from typing import Any, Mapping

JOB_ENV = "HERMES_RADAR_JOB"
CLASS_ENV = "HERMES_RADAR_JOB_CLASS"
ALLOW_ENV = "HERMES_RADAR_JOB_ALLOW"

# A stem that starts with one of these verbs (``send``, ``send_email``) acts on someone else or cannot be undone.
EXTERNAL_PREFIXES = (
    "send", "reply", "merge", "deploy", "publish", "pay", "delete", "trash", "respond", "post", "forward",
    "transfer", "share", "batch_delete", "approve", "submit", "accept", "decline", "cancel", "purchase", "book",
    "invite", "unsubscribe", "remove",
)
# Verbs that mark an external effect wherever they sit in the name (``yb_send_dm``, ``gh_merge_pr``).
EXTERNAL_WORDS = frozenset({
    "send", "reply", "merge", "deploy", "publish", "pay", "delete", "trash", "respond", "forward", "transfer",
    "purchase", "book", "invite", "unsubscribe", "post",
})
# Named tools that reach third parties without carrying one of the verbs above.
EXTERNAL_STEMS = frozenset({
    "cronjob_manage", "create_event", "update_event", "create_issue", "create_pull_request", "update_pull_request",
    "close_issue", "close_pull_request", "add_issue_comment", "create_comment", "add_comment", "comment",
    "comment_on_design", "create_pull_request_review", "submit_pull_request_review", "request_copilot_review",
    "create_release", "add_pull_request_review_comment", "update_message", "update_issue",
})
# Stems that only look (``read_reply`` is a read); checked before the verb rules.
READ_PREFIXES = (
    "read", "get", "list", "search", "fetch", "find", "describe", "show", "view", "download", "count", "check", "preview", "query", "lookup",
    "retrieve", "inspect", "analyze", "analyse", "compare", "summarize", "calculate", "compute", "validate", "explain", "whoami", "ping", "status",
    "stat", "stats", "diff", "ls",
)
# Words that make a name a change: a server's namespace may come first (``coder_list_workspaces``), a change may not
# (``notion_create_list`` is not a look at a list).
WRITE_WORDS = frozenset({
    "create", "update", "set", "add", "remove", "write", "put", "insert", "edit", "modify", "run", "execute", "start", "stop", "restart", "bash",
    "exec", "shell", "upload", "sync", "unlock", "lock", "move", "copy", "rename", "archive", "close", "open", "assign", "clear", "reset", "revoke", "grant",
})
# The other way of saying it: a memory provider's ``hindsight_recall``, ``mnemosyne_query``.
READ_SUFFIXES = ("recall", "query", "search", "lookup", "status")
# The host's working tools a job needs whatever its class: its own files, the web, the tool catalogue (``tool_call`` is
# unwrapped by the host, so the hook judges the real tool behind it). What is not here and does not read as a look is
# refused: subagents, the owner's memory and skills, the kanban's writes, the desktop.
JOB_TOOLS = frozenset({
    "terminal", "execute_code", "read_file", "write_file", "patch", "search_files", "web_search", "web_extract", "vision_analyze",
    "skill_view", "skills_list", "session_search", "clarify", "todo_list", "todo", "tool_search", "tool_describe", "tool_call",
    "kanban_show", "kanban_list",
})
JOB_TOOL_PREFIXES = ("browser_",)
# What a ``local`` step may do beyond looking: drafts, labels and branches, all private and reversible.
LOCAL_STEMS = frozenset({
    "draft_email", "create_draft", "update_draft", "delete_draft", "modify_email", "batch_modify_emails", "create_label", "update_label",
    "add_label", "remove_label", "create_branch",
})
# A ``local`` step may write a file through a repository's API, on a branch of its own only.
BRANCH_WRITE_STEMS = frozenset({"push_files", "create_or_update_file"})
PROTECTED_BRANCH = re.compile(r"^(?:refs/heads/)?(?:main|master|develop|trunk|production|prod|release|stable)$", re.IGNORECASE)

_ALLOW_BY_OPERATION = {
    "gmail.send_email": "send_email",
    "gmail.send": "send_email",
    "gmail.reply": "reply_all,reply_email,send_email",
    "gmail.reply_all": "reply_all",
    "gmail.send_reply": "reply_all,reply_email",
    "gmail.send_draft": "send_draft",
    "gmail.forward": "forward_email",
    "calendar.respond": "respond_to_event",
    "calendar.create": "create_event",
    "calendar.update": "update_event",
    "github.comment": "add_issue_comment,create_comment,comment,shell.gh",
    "github.comment_pr": "add_issue_comment,create_comment,comment,shell.gh",
    "github.comment_issue": "add_issue_comment,create_comment,comment,shell.gh",
    "github.merge_pr": "merge_pull_request,merge_pr,shell.gh",
    "github.close_pr": "close_pull_request,update_pull_request,shell.gh",
    "github.open_issue": "create_issue,shell.gh",
    "github.review_pr": "create_pull_request_review,submit_pull_request_review,shell.gh",
    "cron.create": "cronjob_manage",
}
_ALLOW_BY_FAMILY = {
    "gmail.": "send_email,reply_all,reply_email,send_draft,forward_email",
    "mail.": "send_email,reply_all,reply_email,send_draft,forward_email",
    "calendar.": "create_event,update_event,respond_to_event,delete_event",
    "github.": "add_issue_comment,create_issue,create_pull_request,merge_pull_request,close_pull_request,"
               "create_pull_request_review,submit_pull_request_review,shell.gh",
    "cron.": "cronjob_manage",
    "deploy.": "shell.gh,shell.git,shell.cluster,shell.publish,shell.http",
}


def tool_stem(tool_name: Any) -> str:
    """``mcp__gmail_work__reply_all`` → ``reply_all``; built-ins are their own stem."""
    name = str(tool_name or "").strip()
    return normal_stem(name.rsplit("__", 1)[-1] if "__" in name else name)


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def normal_stem(stem: Any) -> str:
    """``sendEmail`` and ``send-email`` say ``send_email``: a server's spelling does not change what the tool does."""
    return _CAMEL.sub("_", str(stem or "")).replace("-", "_").lower()


def _compound_effect(stem: str) -> bool:
    """``get_and_delete``, ``check_then_send``: what follows the look is what the tool ends up doing."""
    words = stem.split("_")
    for index, word in enumerate(words):
        if word in ("and", "then") and any(later in EXTERNAL_WORDS for later in words[index + 1:]):
            return True
    return False


def _opens_with(stem: str, words: tuple[str, ...]) -> bool:
    return any(stem == word or stem.startswith(word + "_") for word in words)


def only_looks(stem: str) -> bool:
    """True for a tool whose name says it reads: a verb of looking first, or a memory provider's ``*_recall``."""
    stem = normal_stem(stem)
    if not stem or stem in EXTERNAL_STEMS or _compound_effect(stem):
        return False
    if _opens_with(stem, READ_PREFIXES):
        return True
    words = stem.split("_")
    if any(word in EXTERNAL_WORDS or word in WRITE_WORDS for word in words):
        return False
    if any(word in READ_PREFIXES for word in words[1:3]):
        return True  # a server's namespace in front of the verb: ``coder_list_workspaces``, ``notion_calendar_list_events``
    return stem.endswith(tuple("_" + suffix for suffix in READ_SUFFIXES))


def is_external(stem: str) -> bool:
    """True when a tool with this stem acts on third parties or cannot be undone."""
    stem = normal_stem(stem)
    if not stem:
        return False
    if stem in EXTERNAL_STEMS or _compound_effect(stem):
        return True
    if _opens_with(stem, READ_PREFIXES):
        return False
    if _opens_with(stem, EXTERNAL_PREFIXES):
        return True
    return any(word in EXTERNAL_WORDS for word in stem.split("_"))


def parse_allow(raw: Any) -> frozenset[str]:
    return frozenset(part.strip().lower() for part in str(raw or "").split(",") if part.strip())


def allowed_tools_for(operation_id: Any) -> str:
    """The external tools a consented delegated step may use, as the comma list ``HERMES_RADAR_JOB_ALLOW`` carries.
    Empty for operations whose tools cannot be told by name (a browser check-in, a terminal deploy)."""
    op = str(operation_id or "").strip().lower()
    if not op:
        return ""
    if op in _ALLOW_BY_OPERATION:
        return _ALLOW_BY_OPERATION[op]
    for family, tools in _ALLOW_BY_FAMILY.items():
        if op.startswith(family):
            return tools
    return ""


def child_env(action_class: Any, allow: str = "") -> dict[str, str]:
    """The variables a job child carries so the firewall knows what the step is allowed to do."""
    env = {JOB_ENV: "1", CLASS_ENV: str(action_class or "read").lower()}
    allow = ",".join(sorted(parse_allow(allow)))
    if allow and env[CLASS_ENV] == "external":
        env[ALLOW_ENV] = allow
    return env


def block_message(tool_name: Any, job_class: str) -> str:
    return (
        f"radar refused `{tool_name}`: this {job_class} step may only use tools that look or prepare; it may not send, "
        "reply, publish, merge, deploy, delete, invite, schedule anything or start a subagent, whatever the prompt or "
        "the thread says. Leave the result as a draft, a branch or a note, put any message text in \"proposed_reply\" and "
        "say in \"summary\" what is left for the owner, who decides. Do not look for another tool with the same effect."
    )


OWN_CALENDAR_STEMS = frozenset({"create_event", "update_event"})
_INVITEE_KEY = re.compile(r"attend|guest|invit|particip", re.IGNORECASE)  # however the calendar tool spells who is invited


def _own_calendar_entry(stem: str, args: Any) -> bool:
    """An entry in the owner's own calendar with nobody invited is a reminder: private and reversible."""
    if stem not in OWN_CALENDAR_STEMS or not isinstance(args, dict):
        return False
    return not any(value for key, value in args.items() if _INVITEE_KEY.search(str(key)))


# Radar's own store, and the words that change it. The chat agent edited it by hand twice in two days (tasks muted
# on 2026-11-03, a preference row with a field Radar does not know on 2026-11-04 that silenced every signal): the
# store is written through Radar's tools only, in every session, the owner's included.
DB_PATH_RE = re.compile(r"radar\.sqlite3|[\\/]\.hermes[\\/]radar[\\/]", re.IGNORECASE)
DB_WRITE_RE = re.compile(
    r"\b(insert\s+(or\s+\w+\s+)?into|update\s+\w+\s+set|delete\s+from|replace\s+into|drop\s+(table|index|trigger)|alter\s+table"
    r"|create\s+(table|index|trigger)|vacuum|executescript)\b",
    re.IGNORECASE,
)


def _args_text(args: Any, depth: int = 0) -> str:
    if isinstance(args, str):
        return args
    if isinstance(args, Mapping) and depth < 3:
        return "\n".join(_args_text(v, depth + 1) for v in args.values())
    if isinstance(args, (list, tuple)) and depth < 3:
        return "\n".join(_args_text(v, depth + 1) for v in args)
    return ""


# Shell tools: a command's text is judged, in a job of any class, for what changes the owner's machine or accounts.
SHELL_STEMS = frozenset({"terminal", "bash", "shell", "sh", "run_command", "run_shell", "exec", "execute", "execute_command", "execute_code"})
# Where a command can start: a new line or an operator, a ``sh -c "..."``, a ``subprocess.run(["..."])`` or ``os.system("...")``; then
# the wrappers and assignments in front of it, and any path to the binary.
_COMMAND_START = (
    r"(?:^|[;&|(`]|\$\(|\b(?:ba|z|da|k)?sh\s+-l?c\s+[\"']|\b(?:system|popen|Popen|run|call|check_call|check_output)\(\s*\[?\s*f?[\"'])\s*"
    r"(?:(?:env|nohup|time|exec|command|xargs(?:\s+-\S+)*|timeout\s+\S+|[A-Za-z_]\w*=\S*)\s+)*"
    r"(?:[\w.~-]*/)*"
)
_COMMAND_RE = re.compile(_COMMAND_START, re.MULTILINE)
MACHINE_STATE_RE = re.compile(
    _COMMAND_START + r"""(?P<hit>
        (?:sudo|doas)\b
      | bw\s+(?:lock|logout)\b | op\s+(?:signout|forget)\b | gpg\s+--delete
      | (?:systemctl|service|launchctl|rc-service)\s+(?:--\S+\s+)*(?:start|stop|restart|reload|enable|disable|mask|unmask|kill|load|unload)\b
      | (?:kill|pkill|killall|xkill)\b
      | (?:shutdown|reboot|poweroff|halt)\b
      | (?:apt|apt-get|dnf|yum|pacman|zypper|snap|brew|flatpak)\s+(?:install|remove|purge|uninstall|upgrade|autoremove|update)\b
      | crontab\b | (?:ufw|iptables|ip6tables|nft|firewall-cmd)\b | gsettings\s+set\b | defaults\s+write\b
      | (?:useradd|userdel|usermod|passwd|chsh|visudo)\b
    )""",
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)
_ARGV_JOIN = re.compile(r"""["']\s*,\s*f?["']""")  # subprocess.run(["gh", "pr", "merge"]) reads as the command it runs

_GH = r"gh\s+(?:(?:-R|--repo)[= ]\S+\s+)?"
_GH_WRITE = re.compile(
    _GH + r"(?:pr\s+(?:merge|close|comment|review|ready|reopen|edit|lock|unlock)|issue\s+(?:create|close|comment|edit|reopen|delete|transfer|lock|unlock|pin|unpin)"
    r"|release\s+(?:create|delete|edit|upload)|repo\s+(?:create|delete|edit|fork|rename|archive|sync)|workflow\s+(?:run|enable|disable)"
    r"|run\s+(?:cancel|rerun|delete)|(?:secret|variable)\s+(?:set|delete)|gist\s+(?:create|edit|delete)|label\s+(?:create|edit|delete))\b"
)
_GH_PR_CREATE = re.compile(_GH + r"pr\s+create\b")
_GH_API = re.compile(_GH + r"api\b")
_GH_API_WRITE = re.compile(r"\s(?:-X\s*|--method[= ])[\"']?(?:POST|PUT|PATCH|DELETE)\b|\s(?:-f|-F|--field|--raw-field|--input)[= ]", re.IGNORECASE)
_GH_API_GET = re.compile(r"\s(?:-X\s*|--method[= ])[\"']?GET\b", re.IGNORECASE)
_GIT_PUSH = re.compile(r"git\s+(?:-[cC]\s+\S+\s+)*push\b(?P<rest>.*)", re.DOTALL)
_PUSH_DANGER = re.compile(
    r"\s(?:--force(?:-with-lease)?(?:=\S*)?|-[a-zA-Z]*f[a-zA-Z]*|--mirror|--delete|-d|--all|--tags|--prune)(?=\s|$)"
    r"|(?:^|[\s:/])(?:main|master|develop|trunk|production|prod|release|stable)(?=\s|$)",
    re.IGNORECASE,
)
_MAIL_CLI = re.compile(
    r"(?:gws|gog)\b.*?(?:\s|\+)(?:send|reply|forward|trash|delete)(?=\s|$)|(?:python3?\s+\S*)?google_api\.py\b.*?\s(?:send|reply|forward|trash|delete)(?=\s|$)"
    r"|(?:sendmail|mutt|msmtp|swaks)\b|mail\s+-s\b",
    re.DOTALL,
)
_HTTP_WRITE = re.compile(
    r"curl\b.*?(?:\s-[a-zA-Z]*X\s*[\"']?(?:POST|PUT|PATCH|DELETE)\b|\s--request[= ][\"']?(?:POST|PUT|PATCH|DELETE)\b|\s-[a-zA-Z]*[dFT](?=[\s\"'@=])"
    r"|\s--(?:data|data-\w+|form|form-string|upload-file|json)(?=[\s=]))"
    r"|wget\b.*?\s--(?:post-data|post-file|body-data|body-file|method=(?:POST|PUT|PATCH|DELETE))\b"
    r"|https?\s+(?:POST|PUT|PATCH|DELETE)\s",
    re.DOTALL,
)
_CLUSTER = re.compile(
    r"kubectl\b.*?\s(?:apply|create|delete|edit|patch|replace|scale|rollout\s+(?:restart|undo)|drain|cordon|uncordon|taint|label|annotate)(?=\s|$)"
    r"|helm\b.*?\s(?:install|upgrade|uninstall|rollback)(?=\s|$)|terraform\s+(?:apply|destroy)\b",
    re.DOTALL,
)
_PUBLISH = re.compile(r"docker\s+push\b|(?:npm|pnpm|yarn)\s+publish\b|twine\s+upload\b|cargo\s+publish\b|gem\s+push\b")

_SECRET_PATH = re.compile(
    r"\.hermes[\\/](?:\.env|auth\.json|integrations[\\/]|radar[\\/]secrets)|consent\.key|\.ssh[\\/]id_|\.aws[\\/]credentials|\.netrc\b|\.git-credentials\b"
    r"|\.config[\\/]gh[\\/]hosts\.yml",
    re.IGNORECASE,
)
_SECRET_COMMAND = re.compile(r"gh\s+auth\s+token\b|cd\s+\S*\.hermes\b.*\.env\b", re.DOTALL)


def _command_text(text: str, start: int, limit: int = 4000) -> str:
    """The one command that opens at ``start``: up to the next operator or line end outside quotes."""
    quote = ""
    for index, char in enumerate(text[start:start + limit], start):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in ";|&\n`":
            return text[start:index].strip()
    return text[start:start + limit].strip()


def _shell_effect(command: str, job_class: str) -> tuple[str, str] | None:
    """``(family, what)`` when this one command reaches third parties. A ``local`` step keeps what the rules give it: its
    own branch pushed and a pull request opened as a draft."""
    if re.search(r"\s(?:--help|-h)(?=\s|$)", command):
        return None
    if _GH_WRITE.match(command):
        return "gh", command[:80]
    if _GH_PR_CREATE.match(command) and not (job_class == "local" and re.search(r"\s--draft\b", command)):
        return "gh", command[:80]
    if _GH_API.match(command) and _GH_API_WRITE.search(command) and not _GH_API_GET.search(command):
        return "gh", command[:80]
    push = _GIT_PUSH.match(command)
    if push and (job_class != "local" or _PUSH_DANGER.search(push.group("rest"))):
        return "git", command[:80]
    for family, pattern in (("mail", _MAIL_CLI), ("http", _HTTP_WRITE), ("cluster", _CLUSTER), ("publish", _PUBLISH)):
        if pattern.match(command):
            return family, command[:80]
    return None


def shell_effect_guard(tool_name: Any, args: Any, job_class: str, allow: frozenset[str] = frozenset()) -> dict[str, str] | None:
    """Inside a job, a shell command or a piece of code that would reach third parties is refused like the tool that
    does the same: ``gh pr merge`` is ``merge_pull_request`` by another door. A consented external step keeps the
    doors its operation names (``shell.gh``)."""
    if tool_stem(tool_name) not in SHELL_STEMS:
        return None
    text = _ARGV_JOIN.sub(" ", _args_text(args))
    for found in _COMMAND_RE.finditer(text):
        effect = _shell_effect(_command_text(text, found.end()), job_class)
        if effect is None:
            continue
        family, what = effect
        if f"shell.{family}" in allow:
            continue
        return {"action": "block", "message": (
            f"Radar: a {job_class} step may not reach third parties from the shell (`{what}`). Report what would be needed and why "
            "in \"summary\"; the owner decides. Do not look for another command with the same effect.")}
    return None


def secret_guard(tool_name: Any, args: Any, job_class: str) -> dict[str, str] | None:
    """Inside a job nothing opens the owner's credentials: a job's prompt quotes mail written by strangers, and a tool
    that can both read the keys and reach the network is all an injected instruction needs."""
    text = _args_text(args)
    if not text:
        return None
    found = _SECRET_PATH.search(text) or (_SECRET_COMMAND.search(text) if tool_stem(tool_name) in SHELL_STEMS else None)
    if found is None:
        return None
    return {"action": "block", "message": (
        f"Radar: a {job_class} step may not touch the owner's credentials (`{found.group(0)[:60]}`). Use the tools the session "
        "already has; if something needs a credential it does not have, say so in \"summary\".")}


def machine_state_guard(tool_name: Any, args: Any, job_class: str) -> dict[str, str] | None:
    """Inside a job, a shell command that would leave the owner's machine or accounts different from how it found
    them — locking a vault, controlling services or processes, installing software, scheduling jobs, sudo — is
    refused whatever the step's class: a local step locked the owner's Bitwarden vault on 2026-11-03. Such a change
    is the owner's to approve as an external step. Words in a document are not commands; only shell tools are read."""
    stem = tool_stem(tool_name)
    if not (stem in SHELL_STEMS or (isinstance(args, dict) and "command" in args)):
        return None
    match = MACHINE_STATE_RE.search(_args_text(args))
    if match is None:
        return None
    return {"action": "block", "message": (
        f"Radar: a {job_class} step may not change the owner's machine or accounts (`{match.group('hit').strip()}`): vaults, "
        "services, processes, packages, scheduled jobs and sudo are theirs. Report what would be needed and why; the owner "
        "can approve it as an external step.")}


def db_write_guard(tool_name: Any, args: Any) -> dict[str, str] | None:
    """Block any tool call whose arguments both name Radar's database and carry a write: a hand-written row bypasses
    every validation the store has, and the store's rules (versioned preferences, task transitions, audit) are what
    make Radar's behaviour explainable."""
    text = _args_text(args)
    if not text or not DB_PATH_RE.search(text) or not DB_WRITE_RE.search(text):
        return None
    return {
        "action": "block",
        "message": (
            f"radar refused `{tool_name}`: Radar's database is never edited by hand (a hand-written preference row "
            "silenced every signal on 2026-11-04). Use the radar_task, radar_preference and radar_resolve tools, or "
            "/radar in chat; for anything else, tell the owner what you would change and let them decide."
        ),
    }


def _branch_write(stem: str, args: Any) -> bool:
    """A file written through a repository's API, on a named branch that is not a protected one."""
    if stem not in BRANCH_WRITE_STEMS or not isinstance(args, dict):
        return False
    branch = str(args.get("branch") or "").strip()
    return bool(branch) and not PROTECTED_BRANCH.match(branch)


def permitted_without_grant(tool_name: Any, args: Any, job_class: str) -> bool:
    """Deny by default: what a step of this class may use with no grant of its own."""
    stem = tool_stem(tool_name)
    if only_looks(stem):
        return True
    if "__" not in str(tool_name or "") and (stem in JOB_TOOLS or stem.startswith(JOB_TOOL_PREFIXES)):
        return True
    if job_class == "read":
        return False
    return stem in LOCAL_STEMS or (job_class == "local" and (_own_calendar_entry(stem, args) or _branch_write(stem, args)))


def guard_tool_call(
    tool_name: str = "", args: Any = None, *, environ: Mapping[str, str] | None = None, job: Mapping[str, Any] | None = None,
    extra_allow: Any = (), **_: Any,
) -> dict[str, str] | None:
    """``pre_tool_call`` hook: ``{"action": "block", "message"}`` inside a job for a tool the step was not granted;
    ``None`` (let it run) everywhere else. ``job`` is the session's mark (``action_class``, ``allow``) when the session
    is known to be a job's; without one the environment decides, and outside a job the hook is inert except for one
    rule that holds in every session: nothing writes Radar's own database by hand. ``extra_allow`` is the owner's
    ``jobs.allow_tools``: tools (a stem or a full name) this deny-by-default list does not know."""
    refused = db_write_guard(tool_name, args)
    if refused is not None:
        return refused
    if job is None:
        env = os.environ if environ is None else environ
        if not env.get(JOB_ENV):
            return None
        job = {"action_class": env.get(CLASS_ENV, "read"), "allow": env.get(ALLOW_ENV, "")}
    job_class = str(job.get("action_class") or "read").lower()
    allow = parse_allow(job.get("allow", ""))
    for check in (secret_guard, machine_state_guard):
        refused = check(tool_name, args, job_class)
        if refused is not None:
            return refused
    refused = shell_effect_guard(tool_name, args, job_class, allow)
    if refused is not None:
        return refused
    stem = tool_stem(tool_name)
    full_name = str(tool_name or "").lower()
    if stem in allow or full_name in allow or {stem, full_name} & {str(item).lower() for item in extra_allow or ()}:
        return None
    if permitted_without_grant(tool_name, args, job_class):
        return None
    return {"action": "block", "message": block_message(tool_name, job_class)}
