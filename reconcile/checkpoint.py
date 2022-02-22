"""Performs an SRE checkpoint.

The checks are defined in
https://gitlab.cee.redhat.com/app-sre/contract/-/blob/master/content/process/sre_checkpoints.md

"""
import logging
import re
from functools import partial
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

import requests
from jinja2 import Template
from jira import Issue

from reconcile import queries
from reconcile.utils.constants import PROJ_ROOT
from reconcile.utils.jira_client import JiraClient

DEFAULT_CHECKPOINT_LABELS = ("sre-checkpoint",)

# We reject the full RFC 5322 standard since many clients will choke
# with some carefully crafted valid addresses.
EMAIL_ADDRESS_REGEXP = re.compile(r"^\w+[-\w\.]*@(?:\w[-\w]*\w\.)+\w+")
MAX_EMAIL_ADDRESS_LENGTH = 320  # Per RFC3696

MISSING_DATA_TEMPLATE = (
    PROJ_ROOT / "templates" / "jira-checkpoint-missinginfo.j2"
)


class NowhereToReportError(Exception):
    pass


def url_makes_sense(url: str) -> bool:
    """Guesses whether the URL may have a meaningful document.

    Obvious cases are if the document can be fully downloaded, but we
    also accept that the given document may require credentials that
    we don't have.

    The URL is non-sensical if the server is crashing, the document
    doesn't exist or the specified URL can't be even probed with GET.
    """
    rs = requests.get(url)
    # Codes above NOT_FOUND mean the URL to the document doesn't
    # exist, that the URL is very malformed or that it points to a
    # broken resource
    return rs.status_code < HTTPStatus.NOT_FOUND


def valid_owners(owners: Iterable[Mapping[str, str]]) -> bool:
    """Confirm whether all the owners have a name and a valid email address."""
    return all(
        o["name"]
        and o["email"]
        and EMAIL_ADDRESS_REGEXP.fullmatch(o["email"])
        and len(o["email"]) <= MAX_EMAIL_ADDRESS_LENGTH
        for o in owners
    )


VALIDATORS = {
    "sopsUrl": url_makes_sense,
    "architectureDocument": url_makes_sense,
    "grafanaUrls": lambda x: all(url_makes_sense(y["url"]) for y in x),
    "serviceOwners": valid_owners,
}


def render_template(
    template: Path, name: str, path: str, field: str, bad_value: str
) -> str:
    """Render the template with all its fields."""
    with open(template) as f:
        t = Template(f.read(), keep_trailing_newline=True, trim_blocks=True)
        return t.render(
            app_name=name, app_path=path, field=field, field_value=bad_value
        )


def file_ticket(
    jira: JiraClient,
    field: str,
    app_name: str,
    app_path: str,
    labels: Iterable[str],
    parent: str,
    bad_value: str,
) -> Issue:
    """Return a ticket."""
    if bad_value:
        summary = f"Incorrect metadata {field} for {app_name}"
    else:
        summary = f"Missing metadata {field} for {app_name}"

    i = jira.create_issue(
        summary,
        render_template(
            MISSING_DATA_TEMPLATE, app_name, app_path, field, bad_value
        ),
        labels=labels,
        links=(parent,),
    )
    return i


def adjust_and_report_jira_board(
    board_info: Mapping[str, Any],
    override_board: Optional[str],
    override_jira: Optional[str],
) -> dict[str, Any]:
    """Override a JIRA board, cutting a ticket if necessary."""
    if not board_info:
        msg = "Missing JIRA information from the service."
        if not override_jira:
            msg += (
                " You need to specify a path to a JIRA board in app-interface."
            )
        if not override_board:
            msg += " You need to specify a board name"
        if not override_jira or not override_board:
            raise NowhereToReportError(msg)
    if override_jira and override_board:
        board = queries.get_simple_jira_boards(override_jira)
        if not board:
            raise ValueError(
                f"Path {override_jira} can't be used for this service"
            )
        board[0]["name"] = override_board
        return board[0]
    return board_info


def report_invalid_metadata(
    app: Mapping[str, Any],
    path: str,
    settings: Mapping[str, Any],
    parent: str,
    dry_run: bool = False,
    override_board: Optional[str] = None,
    override_jiradef: Optional[str] = None,
) -> None:
    """Cut tickets for any missing/invalid field in the app.

    During dry runs it will only log the rendered template.

    :param app: App description, as returned by
    queries.JIRA_BOARDS_QUICK_QUERY

    :param path: path in app-interface to said app

    :param settings: app-interface settings (necessary to log into the
    JIRA instance)

    :param parent: parent ticket for this checkpoint

    :param dry_run: whether this is a dry run
    """

    board = app["escalationPolicy"]["channels"]["jiraBoard"]
    #         board = adjust_and_report_jira_board(board, override_board, override_jira)
    #     except ValueError:

    if dry_run:
        do_cut = partial(
            render_template,  # type: ignore
            name=app["name"],
            path=path,
            template=MISSING_DATA_TEMPLATE,
        )
    else:
        jira = JiraClient(board, settings)
        do_cut = partial(
            file_ticket,  # type: ignore
            jira=jira,
            app_name=app["name"],
            labels=DEFAULT_CHECKPOINT_LABELS,
            parent=parent,
            app_path=path,
        )

    for field, validator in VALIDATORS.items():
        value = app.get(field)
        try:
            if not validator(value):  # type: ignore
                i = do_cut(field=field, bad_value=str(value))
                logging.error(
                    f"Reporting bad field {field} with value {value}: {i}"
                )
        except Exception:
            i = do_cut(field=field, bad_value=str(value))
            logging.exception(
                f"Problems with {field} for {app['name']} - reporting {i}"
            )
