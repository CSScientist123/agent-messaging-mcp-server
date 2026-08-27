"""The MCP tool surface over `MessagingCore`.

One `FastMCP` instance is built per adapter (`build_server`'s `name` argument
distinguishes them, e.g. `"messaging-science"` vs `"messaging-notebooklm"`).
Every tool here is a thin, formatting-only wrapper: all business logic lives
in `MessagingCore` (see `messaging_core/core.py`); this module never makes a
decision `MessagingCore` didn't already make.

House rules every tool below follows -- see the module-level docstrings of
`messaging_core.responses` and `messaging_core.errors` for the vocabulary:

1. Every tool returns a formatted string (never a raw dict) built with
   `messaging_core.responses`.
2. A `Rejected` is caught here and turned into `responses.rejected(...)` --
   never allowed to become a stack trace the calling agent has to parse.
3. A `NeedsRemote` is reported honestly: local work already happened (or
   didn't -- see `_needs_remote_body`) and a remote step could not be taken.
   It is never dressed up as success.
4. A `RemoteFailure` -- an adapter's own exception for a remote that exists
   and was supposed to work but didn't (a missing binary, a refused
   connection, an HTTP error) -- is caught here too, after the two above,
   and turned into `_remote_failed_body(...)`. It reads as a failed attempt,
   never as a stack trace and never as a rule-based rejection.
5. `send` is fire-and-forget: it returns a receipt, never a reply, and its
   body always ends with `responses.ANTI_POLL`.
6. Tool docstrings are the whole tool description an agent sees at listing
   time -- what the tool does, what it returns, what to call next, and (in
   an `Args:` section) what each parameter means. No changelog, no rationale.
7. Every tool identifies its caller by `requester_uuid` alone; nothing here
   ever takes a requester title.
"""

from __future__ import annotations


from mcp.server.fastmcp import FastMCP

from extension.base import RemoteFailure
from messaging_core import responses
from messaging_core.core import MessagingCore
from messaging_core.errors import NeedsRemote, Rejected
from polling.server import PollingServer

__all__ = ["build_server", "main"]


# ---------------------------------------------------------------------------
# shared formatting helpers
# ---------------------------------------------------------------------------


def _needs_remote_body(exc: NeedsRemote) -> str:
    """Render a `NeedsRemote` honestly: names the capability, never reads as success.

    `exc.reason` is written by `MessagingCore` itself and already states
    precisely what did or didn't happen locally before the remote step was
    needed (e.g. "Message admitted locally ..." vs "Cannot verify ... without
    a matching remote extension.") -- this just surfaces that verbatim rather
    than re-guessing it.
    """
    return (
        f"[needs remote] {exc.reason}\n\n"
        f"Missing capability: {exc.capability!r}. Nothing here was refused by a rule -- "
        f"this is not a rejection -- but the request cannot finish without a remote "
        f"extension that provides that capability."
    )


def _rejected_body(exc: Rejected) -> str:
    return responses.rejected(exc.message, next_call=exc.next_call)


def _remote_failed_body(exc: RemoteFailure) -> str:
    """Render a `RemoteFailure` as a failed attempt, never as a rule-based rejection.

    `_rejected_body` says a rule said no; this says the remote itself did not
    cooperate -- a missing binary, a refused connection, an HTTP error -- and
    the wording is kept deliberately apart from `responses.rejected`'s own
    ("nothing was changed") so an agent reading it goes looking for what
    broke on the remote side, not for a permission or a grant it is missing.
    """
    return (
        f"[remote failed] {exc}\n\n"
        "The remote did not work the way this adapter needs it to -- this was not "
        "a rule refusing the request. Fix whatever is named above (a missing "
        "binary, a refused connection, an HTTP error) and send the work again."
    )


def _render_partner_hits(hits: list[dict]) -> str:
    if not hits:
        return "No partners matched that query."
    lines = [
        f"- {hit['title']!r} (id={hit['id']}, project_id={hit['project_id']}, "
        f"orchestrator_type={hit['orchestrator_type']!r}, score={hit['score']:.2f}): "
        f"{hit['descr_preview']}"
        for hit in hits
    ]
    return "Matching partners, best first:\n" + "\n".join(lines)


def _render_project_hits(hits: list[dict]) -> str:
    if not hits:
        return "No projects matched that query."
    lines = [
        f"- {hit['title']!r} (id={hit['id']}, source_prefix={hit['source_prefix']!r}, "
        f"project_system_id={hit['project_system_id']!r}, score={hit['score']:.2f})"
        for hit in hits
    ]
    return "Matching projects, best first:\n" + "\n".join(lines)


def _render_status(s: dict) -> str:
    lines = [
        f"Partner {s['title']!r} (id={s['partner_id']}) in project "
        f"{s['project_title']!r} (id={s['project_id']}).",
        f"Role: {s['orchestrator_type'] or 'none'} (hierarchy layer {s['layer']}).",
    ]
    if s["working"] is None:
        lines.append("Not working on anything right now.")
    else:
        w = s["working"]
        resumed = " (resumed)" if w["resumed"] else ""
        lines.append(f"Working on: {w['behavior']}{resumed}, started {w['started_at']}.")
    if s["queued"]:
        # By label, not as one number. "Queue depth 4" says nothing useful;
        # "three [RESEARCH], one [QUERY]" says what happens next and why.
        detail = ", ".join(
            f"{q['count']}x {q['behavior']}" + (f" ({q['paused']} paused)" if q["paused"] else "")
            for q in s["queued"]
        )
        lines.append(f"Queued ({s['queue_depth']}), highest priority first: {detail}.")
    else:
        lines.append("Queue is empty.")
    lines += [
        f"Handshakes out: {s['handshakes_out'] or '(none)'}.",
        f"Handshakes in: {s['handshakes_in'] or '(none)'}.",
    ]
    if s["gemini_budget"] is not None:
        lines.append(
            f"Gemini budget: {s['gemini_budget']['used']} used of "
            f"{s['gemini_budget']['budget_count']} granted."
        )
    return "\n".join(lines)


def _render_permissions(p: dict) -> str:
    """Render a permission report: what the conversation allows, beside what it should.

    Both halves, always. `partner_paths` records the intent and the remote
    holds the reality; a report of one of them leaves a Caller unable to tell
    a missing grant from an unrecorded one, which are opposite problems with
    opposite fixes.
    """
    lines = [f"{p['title']!r} currently allows {len(p['allowed'])} rule(s):"]
    lines += [f"  {rule}" for rule in p["allowed"]] or ["  (none)"]
    recorded = p["recorded"]
    lines.append(
        f"Recorded as intended: {len(recorded['read'])} read, {len(recorded['write'])} write."
    )
    if p["missing"]:
        lines.append(f"Intended but NOT allowed: {', '.join(p['missing'])}.")
    if p["unrecorded"]:
        lines.append(f"Allowed but not recorded here: {', '.join(p['unrecorded'])}.")
    if not p["missing"] and not p["unrecorded"]:
        lines.append("The conversation matches what was intended.")
    return "\n".join(lines)


def _render_read(r: dict) -> str:
    header = (
        f"Inbox of {r['title']!r} (id={r['partner_id']}) -- page {r['page']} "
        f"of page_size {r['page_size']}, {r['total']} message(s) total."
    )
    if not r["messages"]:
        return f"{header}\nNo messages on this page."
    lines = [
        f"  [{m['id']}] {m['created_at']} from {m['from_partner_title']!r} "
        f"{m['behavior']}: {m['body']}"
        for m in r["messages"]
    ]
    return header + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# server construction
# ---------------------------------------------------------------------------


def build_server(*, name: str, core: MessagingCore, polling: PollingServer | None = None) -> FastMCP:
    """Build one MCP server exposing `core`'s capabilities as tools.

    `polling`, when given, adds one extra tool -- `notify_partner_push` -- the
    Polling Server's own push-notification endpoint, not a client tool. Every
    other tool is present regardless of whether `polling` is set.
    """
    mcp = FastMCP(name=name)

    # -- identity / project management --------------------------------------

    @mcp.tool()
    def create_project(title: str, source_prefix: str, project_system_id: str) -> str:
        """Register a new project, verified to exist in the named remote app.

        Returns a receipt naming the new project's id. Call create_partner
        next to add partners to it.

        Args:
            title: The project's unique, server-wide title.
            source_prefix: Which remote family this project belongs to; one
                of "nlm_", "code_", "science_", "gemini_".
            project_system_id: The id this project is known by in the remote
                app itself; verified against that remote before creation.
        """
        try:
            project_id = core.create_project(
                title=title, source_prefix=source_prefix, project_system_id=project_system_id
            )
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        return responses.ok(
            f"Project {title!r} created (id={project_id}, source_prefix={source_prefix!r}).",
            next_call="Call create_partner to add a partner to this project.",
        )

    @mcp.tool()
    def create_partner(
        project_id: int,
        title: str,
        partner_id_in_remote: str,
        descr: str,
        uuid: str | None = None,
    ) -> str:
        """Register a new partner under an existing project, verified against the remote.

        Returns a receipt containing the partner's freshly minted uuid. That
        uuid is this partner's ONLY identity credential from now on -- every
        other tool that acts as this partner needs it as `requester_uuid`, and
        it is never shown again after this call. Call handshake or send once
        ready to communicate (nlm_ partners may be sent to directly, with no
        handshake).

        Args:
            project_id: The id of the project this partner belongs to. A
                partner has no kind of its own -- it takes on its project's
                source_prefix ("nlm_", "code_", "science_", "gemini_").
            title: The partner's unique, server-wide, free-form title -- the
                address other tools reach it by.
            partner_id_in_remote: The id this partner is known by in the
                remote app itself; verified against that remote before
                creation.
            descr: A free-text description of this partner (at most 1200
                characters).
            uuid: An identity to assign instead of generating a fresh one.
                Leave unset in the normal case.
        """
        try:
            result = core.create_partner(
                project_id=project_id,
                title=title,
                partner_id_in_remote=partner_id_in_remote,
                descr=descr,
                uuid=uuid,
            )
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        return responses.ok(
            f"Partner {result['title']!r} created (id={result['id']}, "
            f"project_id={result['project_id']}). Its identity credential is "
            f"uuid={result['uuid']!r} -- record it now; it will not be shown again.",
            next_call="Call handshake (or send, for nlm_ targets) once ready to communicate.",
        )

    @mcp.tool()
    def search_partner(
        requester_uuid: str,
        query_title: str,
        project_id: int | None = None,
        limit: int = 3,
    ) -> str:
        """Fuzzy-search live partners by title. Returns the best matches, best first.

        Args:
            requester_uuid: The caller's own identity; must name a live partner.
            query_title: The (possibly inexact) title to search for.
            project_id: If given, restrict the search to this project only.
            limit: Maximum number of matches to return.
        """
        try:
            hits = core.search_partner(
                requester_uuid=requester_uuid,
                query_title=query_title,
                project_id=project_id,
                limit=limit,
            )
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        return responses.ok(_render_partner_hits(hits))

    @mcp.tool()
    def search_project(requester_uuid: str, query_title: str, limit: int = 3) -> str:
        """Fuzzy-search projects by title. Returns the best matches, best first.

        Args:
            requester_uuid: The caller's own identity; must name a live partner.
            query_title: The (possibly inexact) title to search for.
            limit: Maximum number of matches to return.
        """
        try:
            hits = core.search_project(requester_uuid=requester_uuid, query_title=query_title, limit=limit)
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        return responses.ok(_render_project_hits(hits))

    @mcp.tool()
    def delete_partner(requester_uuid: str, partner_title: str) -> str:
        """Permanently delete a live partner by title.

        If the partner has dependent records (e.g. a gemini budget grant it
        made), this is rejected in favor of archive_sessions -- deletion is
        only for partners nothing else still refers to.

        Args:
            requester_uuid: The caller's own identity; must name a live partner.
            partner_title: The exact title of the partner to delete.
        """
        try:
            result = core.delete_partner(requester_uuid=requester_uuid, partner_title=partner_title)
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        return responses.ok(f"Deleted partner {result['title']!r} (id={result['deleted_id']}).")

    @mcp.tool()
    def delete_project(requester_uuid: str, project_title: str) -> str:
        """Permanently delete a project and every partner it holds.

        Args:
            requester_uuid: The caller's own identity; must name a live partner.
            project_title: The exact title of the project to delete.
        """
        try:
            result = core.delete_project(requester_uuid=requester_uuid, project_title=project_title)
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        return responses.ok(
            f"Deleted project {result['title']!r} (id={result['deleted_id']}) and "
            f"{result['partners_deleted']} partner(s) with it."
        )

    @mcp.tool()
    def claim_orchestrator(requester_uuid: str, project_id: int, orchestrator_type: str) -> str:
        """Claim an orchestrator role for the caller, once and permanently.

        Roles are claimed once and never reassigned or released by this call.

        Args:
            requester_uuid: The caller's own identity; must be a partner of
                `project_id` and must not already hold a role.
            project_id: The project the caller claims this role within.
            orchestrator_type: One of "project-orchestrator",
                "gemini-orchestrator", "bridge-scientist".
                "gemini-orchestrator" may only be claimed inside a science_
                project, by a science_ partner.
        """
        try:
            result = core.claim_orchestrator(
                requester_uuid=requester_uuid,
                project_id=project_id,
                orchestrator_type=orchestrator_type,
            )
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        return responses.ok(
            f"Partner {result['partner_id']} now holds {result['orchestrator_type']!r} "
            f"in project {result['project_id']}."
        )

    @mcp.tool()
    def grant_gemini_budget(requester_uuid: str, grantee_uuid: str, budget_count: int) -> str:
        """Grant (or replace) a gemini-orchestrator's budget of gemini_ handshakes.

        Only the project-orchestrator of the grantee's own project may do
        this. A later call for the same grantee replaces the count outright.

        Args:
            requester_uuid: The caller's own identity; must be the
                project-orchestrator of the grantee's project.
            grantee_uuid: The identity of the gemini-orchestrator receiving
                the grant.
            budget_count: The number of gemini_ partners this
                gemini-orchestrator may handshake to; 0 to 3 inclusive.
        """
        try:
            result = core.grant_gemini_budget(
                requester_uuid=requester_uuid, grantee_uuid=grantee_uuid, budget_count=budget_count
            )
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        return responses.ok(
            f"Granted a gemini budget of {result['budget_count']} to partner "
            f"{result['grantee_id']} (granted by partner {result['granted_by_id']})."
        )

    @mcp.tool()
    def archive_sessions(requester_uuid: str, titles: list[str]) -> str:
        """Archive one or more of the caller's own project's partners, freeing live-partner slots.

        Titles not found, already archived, or belonging to a different
        project are skipped rather than failing the whole call -- the
        response lists exactly which titles were archived and which were
        skipped, and why.

        Args:
            requester_uuid: The caller's own identity; must name a live partner.
            titles: The exact titles of partners to archive.
        """
        try:
            result = core.archive_sessions(requester_uuid=requester_uuid, titles=titles)
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        body = f"Archived {result['archived_count']} of {len(titles)} requested: {result['archived']}."
        if result["skipped"]:
            skipped = ", ".join(f"{s['title']!r} ({s['reason']})" for s in result["skipped"])
            body += f" Skipped: {skipped}."
        return responses.ok(body)

    @mcp.tool()
    def status(requester_uuid: str) -> str:
        """Report the caller's own project, role, queue, working task, handshakes and budget.

        Purely local and read-only; never touches a remote extension.

        Args:
            requester_uuid: The caller's own identity; must name a live partner.
        """
        try:
            result = core.status(requester_uuid=requester_uuid)
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        return responses.ok(_render_status(result))

    @mcp.tool()
    def handshake(requester_uuid: str, partner_title: str) -> str:
        """Open a handshake from the caller to another partner in the same project.

        Required before send can be used against most partners (nlm_ targets
        are the exception -- they need no handshake and may be sent to
        directly). Only an orchestrator may initiate a handshake. code_ and
        gemini_ to gemini_ are never legal in either direction. gemini_ to
        science_ is also never legal -- but the reverse, science_ to
        gemini_, initiated by a gemini-orchestrator, IS legal: it is the
        sanctioned bridge that lets the Claude Science to Antigravity chain
        happen at all.

        Args:
            requester_uuid: The caller's own identity; must be an orchestrator.
            partner_title: The exact title of the partner to handshake with.
        """
        try:
            result = core.handshake(requester_uuid=requester_uuid, partner_title=partner_title)
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        return responses.ok(
            f"Handshake established from partner {result['from_partner_id']} to "
            f"{result['to_partner_title']!r} (id={result['to_partner_id']}, "
            f"handshake_id={result['handshake_id']}).",
            next_call=f"Call send to message {result['to_partner_title']!r}.",
        )

    @mcp.tool()
    def send(
        requester_uuid: str,
        queried_partner_title: str,
        message: str,
        behavior: str,
    ) -> str:
        """Enqueue a message for another partner. Returns a receipt, never a reply.

        The reply -- if any -- arrives later as a push event, not from this
        call. Requires a handshake from the caller to the target, except when
        the target is nlm_ (nlm_ never needs a handshake).

        There is one queue per partner and every message is a push into it, so
        there is no direction to choose. The label decides how urgently the
        message is taken up relative to whatever the partner is already doing:
        [TRUTHFUL-REPORT] outranks [QUERY] and [ERROR], which outrank
        [MESSAGE-RESPONSE], which outranks [RESEARCH].

        This tool never configures permissions. Read/write paths are granted
        in advance with add_permissions, because an approval prompt means the
        grant was already missing when the work started.

        Args:
            requester_uuid: The caller's own identity; must name a live partner.
            queried_partner_title: The exact title of the message's recipient.
            message: The message body.
            behavior: One of "[RESEARCH]", "[QUERY]", "[ERROR]",
                "[MESSAGE-RESPONSE]", "[TRUTHFUL-REPORT]". [IDLE] is not
                accepted here -- it is how interrupt_partner carries an
                interruption, and sending one directly would stop a partner
                without stopping its remote.
        """
        try:
            result = core.send(
                requester_uuid=requester_uuid,
                queried_partner_title=queried_partner_title,
                message=message,
                behavior=behavior,
            )
        except Rejected as exc:
            # `MessagingCore.send` commits the queue row and only THEN calls
            # `advance()`, which is what can raise this. `already_committed`
            # (set on the exception by the code that raised it past that
            # commit) is what tells the two failures apart: read
            # defensively, since that marking may or may not have landed in
            # this checkout yet. When it's set, the message is queued
            # despite the rejection -- saying "nothing changed" here would be
            # false, and an agent that believed it would send the same
            # message again and double it.
            if getattr(exc, "already_committed", False):
                return responses.rejected(
                    exc.message,
                    noop=(
                        "The message IS queued -- advance() failed after the queue "
                        "row was already committed. Do not send it again; that would "
                        "double-send it."
                    ),
                    next_call=exc.next_call,
                )
            return _rejected_body(exc)
        except NeedsRemote as exc:
            # Same commit-then-advance gap as above, on the other exception
            # `advance()` can raise. `_needs_remote_body` renders `exc.reason`
            # verbatim and that text was written for the ordinary case, so
            # the committed case is handled here instead of in the shared
            # helper.
            if getattr(exc, "already_committed", False):
                return (
                    f"[needs remote] {exc.reason}\n\n"
                    f"The message IS queued -- advance() could not finish without a "
                    f"remote extension providing {exc.capability!r}, but the queue row "
                    f"was already committed before that step. Do not send it again; "
                    f"that would double-send it."
                )
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        if polling is not None:
            try:
                polling.ensure_partner_thread(partner_id=result["partner_id"])
            except Exception:
                # Arming is an optimisation, not the guarantee -- the supervisor
                # picks the row up within one interval either way. A receipt already
                # earned must never be turned into a failure by the thing that only
                # makes the answer arrive sooner.
                pass
        delivered = result.get("delivered")
        tail = (
            f"It went straight to work (remote_call_id={result['remote_call_id']!r})."
            if delivered
            else "It is waiting behind higher-priority work."
        )
        return responses.ok(
            f"{behavior} queued for {queried_partner_title!r} (queue depth now "
            f"{result['queue_depth']}). {tail}",
            anti_poll=True,
        )

    @mcp.tool()
    def read(requester_uuid: str, partner_title: str, page: int = 1, page_size: int = 10) -> str:
        """Page through a partner's received [QUERY]/[TRUTHFUL-REPORT] message history.

        An empty page is a real, non-error answer.

        Args:
            requester_uuid: The caller's own identity; must name a live partner.
            partner_title: The exact title of the partner whose inbox to read
                (typically the caller's own title).
            page: 1-indexed page number.
            page_size: Number of messages per page.
        """
        try:
            result = core.read(
                requester_uuid=requester_uuid, partner_title=partner_title, page=page, page_size=page_size
            )
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        return responses.ok(_render_read(result))

    @mcp.tool()
    def interrupt_partner(requester_uuid: str, partner_title: str, reason: str) -> str:
        """Stop a partner by pushing an [IDLE] to the front of its queue.

        Not a special mechanism: [IDLE] simply holds the highest priority, so
        pushing one takes the working slot by construction. Whatever the
        partner was working on is marked paused and stays in its queue, and it
        resumes there once the interruption clears -- which happens when you
        send it the thing it was stopped for.

        Only meaningful for partners that execute; non-executing partners
        (e.g. nlm_) are rejected outright since there is nothing to stop.

        Args:
            requester_uuid: The caller's own identity; must share a project
                with the target.
            partner_title: The exact title of the partner to interrupt.
            reason: Why this partner is being interrupted. It is shown to the
                partner verbatim, so write it for that reader.
        """
        try:
            result = core.interrupt_partner(
                requester_uuid=requester_uuid, partner_title=partner_title, reason=reason
            )
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        displaced = result["displaced"]
        what = f"Its {displaced} is paused and will resume." if displaced else "It was idle."
        return responses.ok(
            f"Partner {partner_title!r} (id={result['partner_id']}) is stopped. {what}",
            next_call="Send the partner what it was stopped for; that is what resumes it.",
        )

    @mcp.tool()
    def extend_project(requester_uuid: str, project_title: str) -> str:
        """Declare another project an extension of this caller's project.

        A project holds a limited number of live partners, and that ceiling is
        deliberate. Research too large for one project therefore needs a
        second project explicitly linked to the first, not a larger ceiling.
        Once linked, partners under the two may handshake across the boundary
        -- but only sideways, between two partners holding the SAME
        orchestrator role. It grants nothing between partners of one project.

        Args:
            requester_uuid: The caller's own identity; must hold
                project-orchestrator.
            project_title: The exact title of the project to link.
        """
        try:
            result = core.extend_project(
                requester_uuid=requester_uuid, project_title=project_title
            )
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        if result["already_linked"]:
            return responses.nothing_new(
                f"Projects {result['project_a']} and {result['project_b']} were already linked."
            )
        return responses.ok(
            f"Projects {result['project_a']} and {result['project_b']} are now extensions of "
            "one another. Same-role partners under them may handshake."
        )

    # -- permissions ---------------------------------------------------------
    #
    # Three tools, and send is deliberately not one of them. A permission
    # prompt means the grant was missing BEFORE the work started, so
    # configuring paths as a side effect of sending work is always one step
    # too late. Configure first, then send.

    @mcp.tool()
    def get_permissions(requester_uuid: str, partner_title: str) -> str:
        """Report what an Antigravity conversation currently allows, and what it should.

        Both, because they can differ and the difference is the only thing you
        can act on. Only Antigravity conversations carry path permissions; a
        NotebookLM source never executes and a Claude Science frame has no
        per-frame path concept, so both are refused rather than answered with
        an empty list that would look like a real answer.

        Args:
            requester_uuid: The caller's own identity.
            partner_title: The exact title of the conversation to inspect.
        """
        try:
            result = core.get_permissions(
                requester_uuid=requester_uuid, partner_title=partner_title
            )
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        return responses.ok(_render_permissions(result))

    @mcp.tool()
    def add_permissions(
        requester_uuid: str,
        partner_title: str,
        read_paths: list[str] | None = None,
        write_paths: list[str] | None = None,
    ) -> str:
        """Grant read/write paths to an Antigravity conversation, before sending it work.

        Write paths must include files that do not exist yet but that the
        partner is expected to create. A grant covering only what is already
        on disk guarantees a prompt the first time it writes something new,
        and a prompt is an error in this design, not a question anything will
        answer.

        Adding a path the conversation already holds is not an error.

        Args:
            requester_uuid: The caller's own identity.
            partner_title: The exact title of the conversation to grant to.
            read_paths: Paths it may read.
            write_paths: Paths it may write, existing or not.
        """
        try:
            result = core.add_permissions(
                requester_uuid=requester_uuid,
                partner_title=partner_title,
                read_paths=read_paths,
                write_paths=write_paths,
            )
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        granted = ", ".join(result["granted"]) or "(nothing new)"
        return responses.ok(
            f"Granted to {partner_title!r}: {granted}. "
            f"{len(result['unchanged'])} already present. It now allows "
            f"{len(result['allowed'])} rule(s).",
            next_call="Call send now that the paths are in place.",
        )

    @mcp.tool()
    def delete_permissions(requester_uuid: str, partner_title: str, paths: list[str]) -> str:
        """Revoke paths from an Antigravity conversation.

        This exists because granting alone cannot correct a permission set: a
        path granted by mistake would otherwise outlive every attempt to
        withdraw it. A path is revoked in both directions -- read and write --
        since `paths` names filesystem locations rather than individual
        grants.

        Removing a path the conversation does not hold is not an error.

        Args:
            requester_uuid: The caller's own identity.
            partner_title: The exact title of the conversation to revoke from.
            paths: The filesystem paths to withdraw.
        """
        try:
            result = core.delete_permissions(
                requester_uuid=requester_uuid, partner_title=partner_title, paths=paths
            )
        except Rejected as exc:
            return _rejected_body(exc)
        except NeedsRemote as exc:
            return _needs_remote_body(exc)
        except RemoteFailure as exc:
            return _remote_failed_body(exc)
        revoked = ", ".join(result["revoked"]) or "(nothing was held)"
        return responses.ok(
            f"Revoked from {partner_title!r}: {revoked}. It now allows "
            f"{len(result['allowed'])} rule(s)."
        )

    # -- the Polling Server's own endpoint, not a client tool ----------------

    if polling is not None:

        @mcp.tool()
        def notify_partner_push(partner_uuid: str) -> str:
            """Ensure a drain thread is running for a partner. This is the Polling Server's own endpoint.

            Not a client tool: nothing an agent calls to get work done. A
            remote's push notification (or an equivalent trigger) calls this
            to make sure the given partner's queued work is being drained.
            Idempotent -- calling it again while a drain thread is
            already running is a no-op.

            Args:
                partner_uuid: The identity of the partner whose queue should
                    be drained. Always a uuid, never a title.
            """
            try:
                return polling.notify_partner_push(partner_uuid=partner_uuid)
            except Rejected as exc:
                return _rejected_body(exc)
            except NeedsRemote as exc:
                return _needs_remote_body(exc)
            except RemoteFailure as exc:
                return _remote_failed_body(exc)

    return mcp


def main() -> None:
    """Build a server from environment configuration and run it over stdio.

    Both halves of the stack are built, and the Polling Server is started before
    the transport opens. Starting it is what resumes drain threads for Partners
    that still had work in flight when this process last stopped -- their
    `drain_threads` rows survive precisely so a restart can pick them up.

    Passing `polling` to `build_server` is also what registers
    `notify_partner_push`. Without it a remote's push notification has nothing
    to call, and the queue is drained only as a side effect of somebody else
    sending a message.
    """
    from mcp_server.config import build_stack_from_env, source_prefix_from_env

    source_prefix = source_prefix_from_env()
    core, polling = build_stack_from_env()
    polling.start()
    name = f"messaging-{source_prefix.rstrip('_')}"
    server = build_server(name=name, core=core, polling=polling)
    try:
        server.run(transport="stdio")
    finally:
        # Signal and join the drain threads. This deliberately leaves their
        # `drain_threads` rows in place: a row is what tells the next start()
        # that this Partner had work in flight, and deleting it here would
        # strand exactly the work it exists to protect.
        polling.stop()


if __name__ == "__main__":
    main()
